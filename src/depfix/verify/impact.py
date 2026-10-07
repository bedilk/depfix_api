"""Plan-time local impact testing: does this breaking change actually break
*this* repo?

Runs during ``depfix plan`` (and ``--dry-run`` pipeline passes), before any
fix is generated: run the repo's test suite against its current dependency
tree, bump the affected dependency to the post-change version, install, run
the suite again, and diff by test identity. The manifest edits are reverted
afterwards -- the checkout leaves this function looking exactly as it
entered (module-level dependency trees excepted; the verifier's own
install step restores those from the reverted lockfile).

Three honest outcomes, and the report never claims more than it measured:

- **confirmed breaking** -- the bump made previously-passing tests fail (or
  the install itself broke). The strongest possible evidence that a fix is
  worth generating, quoted in the plan output.
- **not observed breaking** -- the suite passes with the new version too.
  Explicitly NOT "safe to skip": most suites mock the network, so a clean
  run frequently proves only that the mocks don't exercise the changed
  API. The report says so.
- **could not measure** -- no tests, no manifest pin to bump, degraded
  parser, crashed run. Reported as skipped, never guessed.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from depfix.apply.workspace import WorkspaceEditor
from depfix.clone import Checkout
from depfix.core.models import BreakingChange
from depfix.verify.manager import install_dependencies
from depfix.verify.runner import has_test_script, run_tests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImpactReport:
    """Outcome of one plan-time impact check."""

    ran: bool
    skipped_reason: str = ""
    baseline_failure_count: int = 0
    bumped_failure_count: int = 0
    new_failing_tests: tuple[str, ...] = ()
    install_failed_after_bump: bool = False
    degraded: bool = False  # aggregate-count parser; identities unreliable

    @property
    def confirmed_breaking(self) -> bool:
        return self.install_failed_after_bump or bool(self.new_failing_tests)

    def summary(self) -> str:
        if not self.ran:
            return f"impact check skipped: {self.skipped_reason}"
        if self.install_failed_after_bump:
            return "impact check: dependency install FAILED after bumping to the new version -- change confirmed disruptive"
        if self.degraded and self.new_failing_tests:
            return (
                "impact check: failure count increased on the new version, but the test "
                "reporter only yielded aggregate counts -- likely breaking, exact tests unknown"
            )
        if self.new_failing_tests:
            named = ", ".join(self.new_failing_tests[:5])
            more = (
                f" (+{len(self.new_failing_tests) - 5} more)"
                if len(self.new_failing_tests) > 5
                else ""
            )
            return (
                f"impact check: {len(self.new_failing_tests)} test(s) newly fail on the new "
                f"version -- change confirmed breaking for this repo [{named}{more}]"
            )
        if self.degraded:
            return (
                "impact check: no failure-count increase on the new version, but the test "
                "reporter only yielded aggregate counts -- treat as weak evidence"
            )
        return (
            "impact check: test suite still passes on the new version -- NOT proof the repo "
            "is unaffected (mocked externals don't exercise the changed API), only that its "
            "own tests don't observe the break"
        )


def run_impact_check(
    checkout: Checkout,
    change: BreakingChange,
    *,
    install_timeout: float,
    test_timeout: float,
    ignore_scripts: bool = True,
    max_output_bytes: int = 2_000_000,
    ecosystem: str = "javascript",
    max_seconds: float | None = None,
) -> ImpactReport:
    """Measure the change's real effect on this checkout's test suite.

    Only ever called on a disposable checkout (the orchestrator's clone);
    refuses a non-temporary one outright rather than mutating a user's
    working tree even transiently.
    """
    root = Path(checkout.path)
    started = time.monotonic()

    def budget_exhausted(phase: str) -> ImpactReport | None:
        if max_seconds is None or time.monotonic() - started < max_seconds:
            return None
        logger.warning("impact check budget (%.0fs) exhausted before %s", max_seconds, phase)
        return ImpactReport(
            ran=False, skipped_reason=f"budget of {max_seconds:.0f}s exhausted before {phase}"
        )

    if not checkout.is_temporary:
        return ImpactReport(ran=False, skipped_reason="checkout is not disposable")
    if ecosystem not in ("javascript", "python", "ruby", "go"):
        return ImpactReport(ran=False, skipped_reason=f"no impact runner for {ecosystem}")
    if not has_test_script(root, ecosystem):
        return ImpactReport(ran=False, skipped_reason="repo has no test setup")

    bump_edits = _manifest_bump_contents(root, change, ecosystem)
    if not bump_edits:
        return ImpactReport(
            ran=False,
            skipped_reason="no manifest pin found to bump to the new version",
        )

    logger.info("impact check: installing current dependencies for %s", change.package)
    install = install_dependencies(
        root,
        timeout=install_timeout,
        ignore_scripts=ignore_scripts,
        max_output_bytes=max_output_bytes,
        ecosystem=ecosystem,
    )
    if not install.ok:
        return ImpactReport(
            ran=False,
            skipped_reason=f"baseline install failed ({install.skipped_reason or 'install error'})",
        )
    if (over := budget_exhausted("the baseline test run")) is not None:
        return over
    logger.info("impact check: running the baseline test suite")
    baseline = run_tests(
        root, timeout=test_timeout, max_output_bytes=max_output_bytes, ecosystem=ecosystem
    )
    if baseline.crashed:
        return ImpactReport(ran=False, skipped_reason="baseline test run crashed")

    editor = WorkspaceEditor(checkout)
    touched: list[str] = []
    try:
        for relpath, bumped_content in bump_edits.items():
            editor.write_fix(relpath, bumped_content)
            touched.append(relpath)
        if ecosystem == "javascript":
            _regenerate_js_lockfile(
                checkout,
                editor,
                touched,
                timeout=install_timeout,
                ignore_scripts=ignore_scripts,
            )
        if ecosystem == "ruby":
            _regenerate_ruby_lockfile(checkout, editor, touched, timeout=install_timeout)
        if ecosystem == "go":
            _regenerate_go_lockfile(checkout, editor, touched, timeout=install_timeout)

        if (over := budget_exhausted("the bumped install")) is not None:
            return over
        logger.info(
            "impact check: bumping %s to %s and reinstalling", change.package, change.new_version
        )
        bumped_install = install_dependencies(
            root,
            timeout=install_timeout,
            ignore_scripts=ignore_scripts,
            max_output_bytes=max_output_bytes,
            ecosystem=ecosystem,
            frozen=False,
        )
        if not bumped_install.ok:
            return ImpactReport(
                ran=True,
                baseline_failure_count=len(baseline.failed_identities),
                install_failed_after_bump=True,
            )

        if (over := budget_exhausted("the bumped test run")) is not None:
            return over
        logger.info("impact check: re-running the test suite on %s", change.new_version)
        bumped = run_tests(
            root, timeout=test_timeout, max_output_bytes=max_output_bytes, ecosystem=ecosystem
        )
        if bumped.crashed:
            # A suite that *crashes* only on the new version is itself a
            # breaking signal -- report it as such, not as "skipped".
            return ImpactReport(
                ran=True,
                baseline_failure_count=len(baseline.failed_identities),
                install_failed_after_bump=False,
                new_failing_tests=("<test run crashed on the new version>",),
            )

        degraded = baseline.used_fallback_parser or bumped.used_fallback_parser
        if degraded:
            delta = len(bumped.failed_identities) - len(baseline.failed_identities)
            return ImpactReport(
                ran=True,
                baseline_failure_count=len(baseline.failed_identities),
                bumped_failure_count=len(bumped.failed_identities),
                new_failing_tests=("<aggregate failure count increased>",) if delta > 0 else (),
                degraded=True,
            )

        new_failures = sorted(bumped.failed_identities - baseline.failed_identities)
        return ImpactReport(
            ran=True,
            baseline_failure_count=len(baseline.failed_identities),
            bumped_failure_count=len(bumped.failed_identities),
            new_failing_tests=tuple(new_failures),
        )
    finally:
        for relpath in touched:
            try:
                editor.revert(relpath)
            except Exception:
                logger.exception("could not revert %s after impact check", relpath)


def _manifest_bump_contents(root: Path, change: BreakingChange, ecosystem: str) -> dict[str, str]:
    """``{relpath: bumped_content}`` for every manifest pinning the affected
    package below the change's new version."""
    if ecosystem == "javascript":
        from depfix.codemods.lockfile import build_manifest_bumps

        return {
            bump.manifest_relpath: bump.fixed_content for bump in build_manifest_bumps(root, change)
        }
    if ecosystem == "ruby":
        from depfix.ecosystems.ruby_manifests import build_ruby_dependency_drift_bumps

        return {
            bump.manifest_relpath: bump.fixed_content
            for bump in build_ruby_dependency_drift_bumps(root, change)
        }
    if ecosystem == "go":
        from depfix.ecosystems.go_manifests import build_go_dependency_drift_bumps

        return {
            bump.manifest_relpath: bump.fixed_content
            for bump in build_go_dependency_drift_bumps(root, change)
        }
    return _python_requirement_bumps(root, change)


#: Matches one requirements.txt line pinning ``<name>``; group "prefix"
#: preserves the name + extras exactly as spelled.
def _python_requirement_bumps(root: Path, change: BreakingChange) -> dict[str, str]:
    new_version = change.new_version.strip().lstrip("v")
    if not new_version:
        return {}
    out: dict[str, str] = {}
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = root / name
        if not path.is_file():
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pattern = re.compile(
            rf"^(?P<prefix>\s*{re.escape(change.package)}(?:\[[^\]]*\])?)"
            rf"\s*(?:==|~=|>=|<=|<|>)[^;#\n]*(?P<marker>;[^#\n]*)?",
            re.MULTILINE | re.IGNORECASE,
        )

        def _replace(match: re.Match[str]) -> str:
            # A PEP 508 environment marker (`; python_version >= "3.9"`)
            # scopes *whether* the dependency applies -- it must survive
            # the version rewrite verbatim.
            marker = match.group("marker") or ""
            return (
                f"{match.group('prefix')}=={new_version} {marker}".rstrip()
                if marker
                else f"{match.group('prefix')}=={new_version}"
            )

        bumped, count = pattern.subn(_replace, original)
        if count and bumped != original:
            out[name] = bumped
    return out


def _regenerate_js_lockfile(
    checkout: Checkout,
    editor: WorkspaceEditor,
    touched: list[str],
    *,
    timeout: float,
    ignore_scripts: bool,
) -> None:
    """Refresh the npm-family lockfile after the manifest bump so the bumped
    install actually resolves the new version; record every changed lockfile
    with the editor so the ``finally`` revert restores it too."""
    from depfix.codemods.lockfile import lockfile_paths, try_regenerate_lockfile

    before: dict[str, str] = {}
    for lockfile in lockfile_paths(Path(checkout.path)):
        try:
            before[lockfile.relative_to(checkout.path).as_posix()] = lockfile.read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            continue

    ok, message = try_regenerate_lockfile(
        Path(checkout.path), timeout=timeout, ignore_scripts=ignore_scripts
    )
    if not ok:
        logger.debug("impact check: lockfile not regenerated: %s", message)
        return
    for lockfile in lockfile_paths(Path(checkout.path)):
        relpath = lockfile.relative_to(checkout.path).as_posix()
        try:
            after = lockfile.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if relpath in before and after != before[relpath]:
            # Re-write via the editor with the ORIGINAL captured first:
            # write_fix snapshots current-on-disk as "original", so restore
            # the pre-regen text, register the edit, then apply the new one.
            lockfile.write_text(before[relpath], encoding="utf-8")
            editor.write_fix(relpath, after)
            touched.append(relpath)


def _regenerate_ruby_lockfile(
    checkout: Checkout, editor: WorkspaceEditor, touched: list[str], *, timeout: float
) -> None:
    """Refresh Gemfile.lock after the Gemfile bump so the bumped install
    resolves the new version; record the changed lockfile with the editor
    so the ``finally`` revert restores it too."""
    from depfix.ecosystems.ruby_lockfile import ruby_lockfile_paths, try_refresh_ruby_lockfile

    before: dict[str, str] = {}
    for lockfile in ruby_lockfile_paths(Path(checkout.path)):
        try:
            before[lockfile.relative_to(checkout.path).as_posix()] = lockfile.read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            continue

    ok, message = try_refresh_ruby_lockfile(Path(checkout.path), timeout=timeout)
    if not ok:
        logger.debug("impact check: Gemfile.lock not regenerated: %s", message)
        return
    for lockfile in ruby_lockfile_paths(Path(checkout.path)):
        relpath = lockfile.relative_to(checkout.path).as_posix()
        try:
            after = lockfile.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if relpath in before and after != before[relpath]:
            lockfile.write_text(before[relpath], encoding="utf-8")
            editor.write_fix(relpath, after)
            touched.append(relpath)


def _regenerate_go_lockfile(
    checkout: Checkout, editor: WorkspaceEditor, touched: list[str], *, timeout: float
) -> None:
    """Refresh go.sum so the bumped install can resolve at all, recording any
    changed file with the editor so the ``finally`` revert restores it too."""
    from depfix.ecosystems.go_lockfile import go_generated_paths, try_refresh_go_lockfile

    root = Path(checkout.path)
    before: dict[str, str] = {}
    for generated in go_generated_paths(root):
        try:
            before[generated.relative_to(root).as_posix()] = generated.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue

    ok, message = try_refresh_go_lockfile(root, timeout=timeout)
    if not ok:
        logger.debug("impact check: go.sum not regenerated: %s", message)
        return
    for relpath, original_text in before.items():
        path = root / relpath
        try:
            refreshed = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if refreshed == original_text:
            continue
        path.write_text(original_text, encoding="utf-8")
        editor.write_fix(relpath, refreshed)
        if relpath not in touched:
            touched.append(relpath)
