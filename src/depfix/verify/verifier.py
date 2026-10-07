"""Orchestrates evidence-based verification of candidate fixes.

The core comparison is baseline-vs-after-fix by test *identity*, not by
pass/fail count -- a repo with 3 pre-existing failing tests and a fix that
introduces a 4th distinct failure must not be reported as "still 3
failures, looks fine" just because some other flaky test happened to start
passing in the same run. See :class:`depfix.verify.models.TestRunResult`.

Week 7: the test suite is no longer the *only* oracle. Every path that used
to dead-end at "mark everything SUSPECT" -- no test script, install
failure, unparseable reporter output, crashed run -- now falls through to
:mod:`depfix.verify.typecheck`, which applies the identical
baseline-vs-after discipline to ``tsc --noEmit`` diagnostics. Each terminal
path records which oracle actually decided, as a
:class:`depfix.verify.confidence.ConfidenceTier`, so a caller can open a
draft PR for weak evidence instead of throwing the fix away.

``skipped_reason`` keeps its original meaning throughout: *why the repo's
own tests did not decide*. It is not overwritten by typecheck outcomes --
those live in ``typecheck_baseline``/``typecheck_after``.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import tempfile
from pathlib import Path

from depfix.apply.models import EditVerdict, FileEdit
from depfix.apply.workspace import WorkspaceEditor
from depfix.core.models import BreakingChange
from depfix.verify.attribution import AttributionResult, attribute_failures
from depfix.verify.characterize import CharacterizationReport, Characterizer
from depfix.verify.confidence import ConfidenceTier
from depfix.verify.contract import ContractReport
from depfix.verify.contract import compare as compare_contracts
from depfix.verify.coverage import CoverageReport, check_changed_lines
from depfix.verify.manager import InstallResult, install_dependencies
from depfix.verify.models import StageResult, StageStatus, TestRunResult
from depfix.verify.runner import has_test_script, run_tests
from depfix.verify.sandbox import DEFAULT_MAX_OUTPUT_BYTES
from depfix.verify.selection import select_test_files, select_tests_for_changes
from depfix.verify.static_check import (
    StaticCheckResult,
    new_static_diagnostics,
    run_static_check,
)
from depfix.verify.typecheck import TypecheckResult, new_diagnostics, run_typecheck

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class VerificationReport:
    """Outcome of one :meth:`Verifier.verify` call."""

    ran: bool
    #: Why the *repo's own test suite* did not settle the verdicts. Empty
    #: when tests ran and were trusted. Never carries typecheck detail --
    #: see ``typecheck_after``.
    skipped_reason: str = ""
    install: InstallResult | None = None
    baseline: TestRunResult | None = None
    after_fix: TestRunResult | None = None
    edits: tuple[FileEdit, ...] = ()
    attribution: AttributionResult | None = None
    typecheck_baseline: TypecheckResult | None = None
    typecheck_after: TypecheckResult | None = None
    #: Which oracle actually decided these verdicts.
    tier: ConfidenceTier = ConfidenceTier.NONE
    #: One line naming that oracle, for the PR body and CLI.
    tier_reason: str = ""
    characterization: CharacterizationReport | None = None
    #: Did the suite actually execute the migrated lines? (Step 2)
    coverage: CoverageReport | None = None
    #: Did the migrated call produce the same HTTP request? (Step 1)
    contract: ContractReport | None = None
    #: Non-TypeScript static oracle, when one applied. (Step 6)
    static_baseline: StaticCheckResult | None = None
    static_after: StaticCheckResult | None = None
    #: New failures that did NOT reproduce on every re-run -- reported, but
    #: never blamed on the fix. (Step 3)
    flaky_test_identities: tuple[str, ...] = ()
    #: Structured results for always-run stages (smoke, characterization).
    #: Each entry carries status + skip_reason so callers and PR bodies
    #: can surface skips rather than silently hiding them.
    stage_results: tuple[StageResult, ...] = ()

    @property
    def new_failure_count(self) -> int:
        if self.baseline is None or self.after_fix is None:
            return 0
        return len(self.after_fix.failed_identities - self.baseline.failed_identities)

    @property
    def typechecked(self) -> bool:
        return self.typecheck_after is not None and self.typecheck_after.usable

    @property
    def new_diagnostics(self) -> tuple:
        if self.typecheck_baseline is None or self.typecheck_after is None:
            return ()
        return new_diagnostics(self.typecheck_baseline, self.typecheck_after)

    @property
    def new_static_diagnostics(self) -> tuple:
        if self.static_baseline is None or self.static_after is None:
            return ()
        return new_static_diagnostics(self.static_baseline, self.static_after)


class Verifier:
    """Runs a checkout's real test suite -- and, when that can't decide, its
    typechecker -- before/after a batch of candidate fixes, to settle each
    file's verdict.

    Not safe to share across threads or reuse across unrelated fix
    batches -- it drives ``editor`` (see :class:`WorkspaceEditor`)'s
    revert/reapply cycle directly and assumes exclusive ownership of the
    checkout for the duration of :meth:`verify`.
    """

    def __init__(
        self,
        editor: WorkspaceEditor,
        *,
        install_timeout: float = 300.0,
        test_timeout: float = 300.0,
        ignore_scripts: bool = True,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        typecheck_enabled: bool = True,
        typecheck_timeout: float = 300.0,
        characterizer: Characterizer | None = None,
        ecosystem: str = "javascript",
        coverage_enabled: bool = True,
        contract_enabled: bool = False,
        flake_retries: int = 1,
        static_check_enabled: bool = True,
        static_check_timeout: float = 300.0,
        selective_tests: bool = False,
        frozen_install: bool = True,
    ) -> None:
        self._editor = editor
        self._ecosystem = ecosystem
        self._install_timeout = install_timeout
        self._test_timeout = test_timeout
        self._ignore_scripts = ignore_scripts
        self._max_output_bytes = max_output_bytes
        self._typecheck_enabled = typecheck_enabled
        self._typecheck_timeout = typecheck_timeout
        self._characterizer = characterizer
        self._coverage_enabled = coverage_enabled
        self._contract_enabled = contract_enabled
        self._flake_retries = flake_retries
        self._static_check_enabled = static_check_enabled
        self._static_check_timeout = static_check_timeout
        self._selective_tests = selective_tests
        self._frozen_install = frozen_install
        self._evidence_dir: Path | None = None
        self._contract_paths: dict[str, Path] = {}
        self._coverage_dir: Path | None = None
        self._change: BreakingChange | None = None

    def verify(
        self, edits: list[FileEdit], *, change: BreakingChange | None = None
    ) -> VerificationReport:
        """Public entry point: allocate a scratch dir for evidence artifacts
        (V8 coverage output, HTTP contract recordings), run the real
        verification, then always clean it up.

        The artifacts are deliberately temporary: they are inputs to a
        verdict, not something to leave in a customer's checkout.
        """
        self._evidence_dir = Path(tempfile.mkdtemp(prefix="depfix-evidence-"))
        self._contract_paths = {}
        self._coverage_dir = None
        try:
            return self._verify_body(edits, change=change)
        finally:
            shutil.rmtree(self._evidence_dir, ignore_errors=True)
            self._evidence_dir = None

    def _verify_body(
        self, edits: list[FileEdit], *, change: BreakingChange | None = None
    ) -> VerificationReport:
        """Settle each on-disk edit to KEPT / REVERTED / SUSPECT, using the
        strongest oracle that can actually reach a verdict.

        Edits not currently on disk (``SKIPPED``/already ``REVERTED``) pass
        through unchanged -- there's nothing on the filesystem for any
        oracle to confirm or deny about them.
        """
        self._change = change
        on_disk = [e for e in edits if e.is_on_disk]
        if not on_disk:
            return VerificationReport(
                ran=False,
                skipped_reason="no on-disk edits to verify",
                edits=tuple(edits),
                tier=ConfidenceTier.NONE,
                tier_reason="no edits were written to disk",
            )

        root = self._editor.checkout.path

        # Gate and runner must agree on test directory: for JS monorepos, check
        # the owning package dir (where tests actually run), not the repo root.
        gate_root = self._test_cwd(on_disk[0].relpath) if on_disk else root
        if not has_test_script(gate_root, self._ecosystem):
            return self._decide_without_tests(
                edits, on_disk, ran=False, skipped_reason="repo has no test script"
            )

        install = install_dependencies(
            root,
            timeout=self._install_timeout,
            ignore_scripts=self._ignore_scripts,
            max_output_bytes=self._max_output_bytes,
            ecosystem=self._ecosystem,
            frozen=self._frozen_install,
        )
        if not install.ok:
            return self._decide_without_tests(
                edits,
                on_disk,
                ran=False,
                skipped_reason=install.skipped_reason or "dependency install failed",
                install=install,
            )

        touched_relpaths = tuple(e.relpath for e in on_disk)
        edit_by_relpath = {e.relpath: e for e in on_disk}

        # Set up coverage and contract tracking infrastructure
        coverage_dir = None
        contract_log_baseline = None
        contract_log_after = None
        if self._coverage_enabled:
            coverage_dir = Path(tempfile.mkdtemp(prefix="depfix-coverage-"))
        if self._contract_enabled:
            contract_log_baseline = (
                Path(tempfile.mkdtemp(prefix="depfix-contract-")) / "baseline.jsonl"
            )
            contract_log_after = Path(tempfile.mkdtemp(prefix="depfix-contract-")) / "after.jsonl"

        # Selective test selection
        selective_test_files = None
        if self._selective_tests:
            changed_files = [Path(root) / relpath for relpath in touched_relpaths]
            selection = select_tests_for_changes(Path(root), changed_files)
            selective_test_files = selection.test_files if selection.test_files else None

        baseline = self._run_tests_on_baseline(
            touched_relpaths,
            install.package_manager,
            coverage_dir=None,  # Don't need baseline coverage
            contract_log_path=contract_log_baseline,
            selective_test_files=selective_test_files,
        )
        after_fix = self._run_tests_on_fixed(
            touched_relpaths,
            edit_by_relpath,
            install.package_manager,
            coverage_dir=coverage_dir,
            contract_log_path=contract_log_after,
            selective_test_files=selective_test_files,
        )

        if baseline.crashed or after_fix.crashed:
            return self._decide_without_tests(
                edits,
                on_disk,
                ran=True,
                skipped_reason="baseline or after-fix run crashed",
                install=install,
                baseline=baseline,
                after_fix=after_fix,
            )

        if baseline.used_fallback_parser or after_fix.used_fallback_parser:
            # Aggregate-count-only comparisons can't reliably tell "same
            # failures" from "different failures with the same count" --
            # see parse_fallback's docstring. Don't trust it to decide
            # which file broke; try the typechecker instead.
            return self._decide_without_tests(
                edits,
                on_disk,
                ran=True,
                skipped_reason="",
                install=install,
                baseline=baseline,
                after_fix=after_fix,
            )

        new_failure_identities = after_fix.failed_identities - baseline.failed_identities
        new_failure_identities, flaky = self._filter_flakes(
            new_failure_identities, touched_relpaths, edit_by_relpath, install.package_manager
        )
        new_failures = [c for c in after_fix.failed_cases if c.identity in new_failure_identities]

        if not new_failures:
            coverage, contract = self._behavioural_evidence(on_disk)
            tier, reason = self._grade_clean_run(coverage, contract)
            settled = tuple(self._settle(e, EditVerdict.KEPT) for e in edits)
            return VerificationReport(
                ran=True,
                install=install,
                baseline=baseline,
                after_fix=after_fix,
                edits=settled,
                tier=tier,
                tier_reason=reason,
                coverage=coverage,
                contract=contract,
                flaky_test_identities=flaky,
            )

        attribution = attribute_failures(
            new_failures, touched_relpaths=touched_relpaths, checkout_root=root
        )

        settled_edits: list[FileEdit] = []
        for edit in edits:
            if not edit.is_on_disk:
                settled_edits.append(edit)
                continue
            if edit.relpath in attribution.by_file:
                self._editor.revert(edit.relpath)
                settled_edits.append(self._settle(edit, EditVerdict.REVERTED))
            elif attribution.unattributed:
                # Some new failure exists that we can't pin on any specific
                # file -- don't clear *this* edit as confirmed-clean while
                # that ambiguity is unresolved.
                settled_edits.append(self._settle(edit, EditVerdict.SUSPECT))
            else:
                settled_edits.append(self._settle(edit, EditVerdict.KEPT))

        decided_cleanly = all(
            e.verdict != EditVerdict.SUSPECT for e in settled_edits if e.is_on_disk
        )
        return VerificationReport(
            ran=True,
            install=install,
            baseline=baseline,
            after_fix=after_fix,
            edits=tuple(settled_edits),
            attribution=attribution,
            tier=ConfidenceTier.HIGH if decided_cleanly else ConfidenceTier.LOW,
            tier_reason=(
                "the repository's own test suite attributed every new failure to a specific file"
                if decided_cleanly
                else "new test failures could not be attributed to a specific file"
            ),
            flaky_test_identities=flaky,
        )

    # -- test-oracle plumbing --------------------------------------------------

    def _run_tests_on_baseline(
        self, touched_relpaths: tuple[str, ...], package_manager: str, **_kwargs
    ) -> TestRunResult:
        """Delegate to the unified phase runner. Kept for callers written
        against the pre-Week-8 signature; every argument goes through
        ``_run_phase`` so the coverage/contract/selective-tests plumbing
        added in Step 8e applies uniformly."""
        for relpath in touched_relpaths:
            self._editor.revert(relpath)
        return self._run_phase("baseline", touched_relpaths, package_manager)

    def _run_tests_on_fixed(
        self,
        touched_relpaths: tuple[str, ...],
        edit_by_relpath: dict[str, FileEdit],
        package_manager: str,
        **_kwargs,
    ) -> TestRunResult:
        """After-fix counterpart of _run_tests_on_baseline. Writes each edit
        back to disk before delegating to _run_phase so the phase runner
        sees the same tree the tests will actually exercise."""
        for relpath in touched_relpaths:
            self._editor.write_fix(relpath, edit_by_relpath[relpath].fixed_content)
        return self._run_phase("after_fix", touched_relpaths, package_manager)

    def _run_phase(
        self, phase: str, touched_relpaths: tuple[str, ...], package_manager: str
    ) -> TestRunResult:
        """One test run, with whichever evidence recorders are enabled.

        Coverage is recorded only for the after-fix run: the question is
        "did the suite execute the *new* lines", and the baseline tree has
        no new lines. Contract recording runs for both, because a diff needs
        two sides.
        """
        root = self._editor.checkout.path
        coverage_dir = None
        contract_out = None

        if (
            self._coverage_enabled
            and self._ecosystem == "javascript"
            and phase.startswith("after_fix")
            and self._evidence_dir is not None
        ):
            coverage_dir = self._evidence_dir / "coverage"
            self._coverage_dir = coverage_dir

        if self._contract_enabled and self._ecosystem == "javascript" and self._evidence_dir:
            contract_out = self._evidence_dir / f"{phase}.contract.jsonl"
            self._contract_paths.setdefault(phase, contract_out)

        only_files: tuple[str, ...] = ()
        if self._selective_tests:
            only_files = select_test_files(root, touched_relpaths, ecosystem=self._ecosystem)

        selective_files = [Path(root) / f for f in only_files] if only_files else None
        return run_tests(
            root,
            timeout=self._test_timeout,
            package_manager=package_manager,
            max_output_bytes=self._max_output_bytes,
            test_cwd=self._test_cwd(touched_relpaths[0]),
            ecosystem=self._ecosystem,
            coverage_dir=coverage_dir,
            contract_log_path=contract_out,
            selective_test_files=selective_files,
        )

    # -- flake handling --------------------------------------------------------

    def _filter_flakes(
        self,
        new_failure_identities: frozenset[str],
        touched_relpaths: tuple[str, ...],
        edit_by_relpath: dict[str, FileEdit],
        package_manager: str,
    ) -> tuple[frozenset[str], tuple[str, ...]]:
        """Re-run the after-fix suite and keep only failures that reproduce.

        One flaky test currently costs a correct fix its verdict: it fails
        once, gets attributed to the edited file, and the edit is reverted.
        A regression caused by a code change reproduces; a flake does not.
        So we re-run and intersect, and report the difference rather than
        silently discarding it -- "your suite is flaky" is useful output.
        """
        if not new_failure_identities or self._flake_retries <= 0:
            return new_failure_identities, ()

        surviving = set(new_failure_identities)
        for attempt in range(self._flake_retries):
            rerun = self._run_phase(
                f"after_fix_retry{attempt + 1}", touched_relpaths, package_manager
            )
            if rerun.crashed or rerun.used_fallback_parser:
                # An unusable re-run is not evidence of anything; keep the
                # original verdict rather than clearing failures on a
                # technicality.
                return new_failure_identities, ()
            surviving &= rerun.failed_identities
            if not surviving:
                break

        flaky = tuple(sorted(new_failure_identities - surviving))
        if flaky:
            logger.info(
                "%d new failure(s) did not reproduce on re-run; treating as flaky: %s",
                len(flaky),
                ", ".join(flaky[:5]),
            )
        return frozenset(surviving), flaky

    # -- behavioural evidence ---------------------------------------------------

    def _behavioural_evidence(
        self, on_disk: list[FileEdit]
    ) -> tuple[CoverageReport | None, ContractReport | None]:
        coverage = None
        if self._coverage_enabled and self._coverage_dir is not None:
            coverage = check_changed_lines(self._coverage_dir, self._editor.checkout.path, on_disk)

        contract = None
        baseline_path = self._contract_paths.get("baseline")
        after_path = self._contract_paths.get("after_fix")
        if baseline_path is not None and after_path is not None:
            contract = compare_contracts(baseline_path, after_path)

        return coverage, contract

    @staticmethod
    def _grade_clean_run(
        coverage: CoverageReport | None, contract: ContractReport | None
    ) -> tuple[ConfidenceTier, str]:
        """Grade a suite that introduced no new failures.

        "No new failures" is the floor, not the ceiling. A run that never
        executed the changed line, or that never made the request the
        migration was about, has not actually observed this fix -- so it
        earns MEDIUM with the reason stated, not HIGH. Worst signal wins.
        """
        base = "the repository's own test suite introduced no new failures"

        if contract is not None and contract.ran and contract.no_traffic:
            return ConfidenceTier.MEDIUM, f"{base}, but {contract.summary()}"
        if coverage is not None and coverage.ran and coverage.any_uncovered:
            return ConfidenceTier.MEDIUM, f"{base}, but {coverage.summary()}"
        if contract is not None and contract.ran and not contract.identical:
            return ConfidenceTier.MEDIUM, f"{base}, but {contract.summary()}"

        strengths = [base]
        if coverage is not None and coverage.ran and not coverage.any_uncovered:
            strengths.append(coverage.summary())
        if contract is not None and contract.identical:
            strengths.append(contract.summary())
        return ConfidenceTier.HIGH, "; ".join(strengths)

    def _test_cwd(self, first_relpath: str):
        """The directory tests run from. Only npm has per-package test
        scripts; Go runs from the module root, pytest discovers its own
        rootdir, and Bundler reads the Gemfile at the top."""
        if self._ecosystem != "javascript":
            return self._editor.checkout.path
        from depfix.scanners.workspace import owning_package_dir

        return owning_package_dir(self._editor.checkout.path, first_relpath)

    # -- typecheck fallback ----------------------------------------------------

    def _decide_without_tests(
        self,
        edits: list[FileEdit],
        on_disk: list[FileEdit],
        *,
        ran: bool,
        skipped_reason: str,
        install: InstallResult | None = None,
        baseline: TestRunResult | None = None,
        after_fix: TestRunResult | None = None,
    ) -> VerificationReport:
        """The tests didn't decide. Try the typechecker; failing that, leave
        every on-disk edit SUSPECT exactly as before.

        ``skipped_reason`` is passed through untouched -- it explains the
        *test* outcome, and a caller (or a test asserting on it) must not
        see it mutate just because a typecheck was attempted afterwards.
        """
        root = self._editor.checkout.path
        # For JS monorepos, typecheck in the owning package dir where tsconfig
        # lives, not the repo root (same directory tests would run from).
        typecheck_root = self._test_cwd(on_disk[0].relpath) if on_disk else root

        def suspect(
            tier_reason: str,
            *,
            resolved_install: InstallResult | None,
            tc_baseline: TypecheckResult | None = None,
            tc_after: TypecheckResult | None = None,
        ) -> VerificationReport:
            from depfix.verify.smoke import SmokeStage, run_smoke_check

            stage_results: list[StageResult] = []

            # --- Characterization: always attempt, emit structured result ----
            characterization = self._characterize_always(on_disk, resolved_install)
            char_stage = self._characterization_stage_result(characterization)
            stage_results.append(char_stage)

            settled = tuple(self._mark_suspect(e) for e in edits)
            tier = ConfidenceTier.LOW
            reason = tier_reason

            if characterization is not None and characterization.confirmed:
                settled = tuple(
                    self._settle(e, EditVerdict.KEPT)
                    if e.relpath == characterization.relpath and e.is_on_disk
                    else e
                    for e in settled
                )
                tier = ConfidenceTier.MEDIUM
                reason = "characterization fixture preserved the pre-migration behaviour"
            else:
                # --- Per-file module-load smoke (existing oracle) ---------------
                smoke_failures: list[str] = []
                for edit in on_disk:
                    smoke = run_smoke_check(typecheck_root, edit.relpath)
                    if smoke.ran and not smoke.passed:
                        self._editor.revert(edit.relpath)
                        settled = tuple(
                            self._settle(candidate, EditVerdict.REVERTED)
                            if candidate.relpath == edit.relpath and candidate.is_on_disk
                            else candidate
                            for candidate in settled
                        )
                        smoke_failures.append(
                            f"{edit.relpath}: {smoke.error or 'module would not load'}"
                        )
                if smoke_failures:
                    reason = f"{tier_reason}; smoke check failed: {'; '.join(smoke_failures)}"

            # --- Whole-app boot smoke: always attempt, structured result ------
            smoke_stage = SmokeStage(self)
            smoke_result = smoke_stage.run(checkout_path=root)
            stage_results.append(smoke_result)
            if smoke_result.blocks_pr:
                reason = f"{reason}; app smoke failed: {smoke_result.detail}"

            try:
                from depfix.obs.metrics import verification_stage_outcomes

                for sr in stage_results:
                    verification_stage_outcomes.record_stage_result(sr)
            except Exception:  # nosec B110 — never let metrics bookkeeping break verification
                pass

            return VerificationReport(
                ran=ran,
                skipped_reason=skipped_reason,
                install=resolved_install,
                baseline=baseline,
                after_fix=after_fix,
                edits=settled,
                typecheck_baseline=tc_baseline,
                typecheck_after=tc_after,
                tier=tier,
                tier_reason=reason,
                characterization=characterization,
                stage_results=tuple(stage_results),
            )

        if not self._typecheck_enabled:
            return suspect("typecheck verification is disabled", resolved_install=install)

        if self._ecosystem != "javascript":
            if not self._static_check_enabled:
                return suspect(
                    f"static checking is disabled for {self._ecosystem}",
                    resolved_install=install,
                )
            return self._decide_with_static_check(
                edits,
                on_disk,
                ran=ran,
                skipped_reason=skipped_reason,
                install=install,
                baseline=baseline,
                after_fix=after_fix,
                suspect=suspect,
            )

        # `tsc` needs node_modules for the SDK's own .d.ts files, so the
        # "no test script" path has to install even though it never intended
        # to run anything.
        resolved_install = install
        if resolved_install is None:
            resolved_install = install_dependencies(
                typecheck_root,
                timeout=self._install_timeout,
                ignore_scripts=self._ignore_scripts,
                max_output_bytes=self._max_output_bytes,
                ecosystem=self._ecosystem,
                frozen=self._frozen_install,
            )
        if not resolved_install.ok:
            return suspect(
                f"could not install dependencies for a typecheck "
                f"({resolved_install.skipped_reason or 'install failed'})",
                resolved_install=resolved_install,
            )

        touched_relpaths = tuple(e.relpath for e in on_disk)
        edit_by_relpath = {e.relpath: e for e in on_disk}

        for relpath in touched_relpaths:
            self._editor.revert(relpath)
        tc_baseline = run_typecheck(
            typecheck_root, timeout=self._typecheck_timeout, max_output_bytes=self._max_output_bytes
        )
        for relpath in touched_relpaths:
            self._editor.write_fix(relpath, edit_by_relpath[relpath].fixed_content)

        if not tc_baseline.usable:
            return suspect(
                f"typecheck unavailable: {tc_baseline.skipped_reason or 'baseline typecheck produced no usable output'}",
                resolved_install=resolved_install,
                tc_baseline=tc_baseline,
            )

        tc_after = run_typecheck(
            typecheck_root, timeout=self._typecheck_timeout, max_output_bytes=self._max_output_bytes
        )
        if not tc_after.usable:
            return suspect(
                f"typecheck unavailable: {tc_after.skipped_reason or 'after-fix typecheck produced no usable output'}",
                resolved_install=resolved_install,
                tc_baseline=tc_baseline,
                tc_after=tc_after,
            )

        added = new_diagnostics(tc_baseline, tc_after)
        if not added:
            settled = tuple(self._settle(e, EditVerdict.KEPT) for e in edits)
            return VerificationReport(
                ran=ran,
                skipped_reason=skipped_reason,
                install=resolved_install,
                baseline=baseline,
                after_fix=after_fix,
                edits=settled,
                typecheck_baseline=tc_baseline,
                typecheck_after=tc_after,
                tier=ConfidenceTier.MEDIUM,
                tier_reason="`tsc --noEmit` introduced no new diagnostics",
            )

        # Unlike a test failure, a type error names its own file -- so
        # attribution here is exact, and only diagnostics landing outside
        # every touched file are ambiguous.
        blamed = {d.relpath for d in added} & set(touched_relpaths)
        unattributed = [d for d in added if d.relpath not in touched_relpaths]

        settled_edits: list[FileEdit] = []
        for edit in edits:
            if not edit.is_on_disk:
                settled_edits.append(edit)
                continue
            if edit.relpath in blamed:
                self._editor.revert(edit.relpath)
                settled_edits.append(self._settle(edit, EditVerdict.REVERTED))
            elif unattributed:
                settled_edits.append(self._settle(edit, EditVerdict.SUSPECT))
            else:
                settled_edits.append(self._settle(edit, EditVerdict.KEPT))

        return VerificationReport(
            ran=ran,
            skipped_reason=skipped_reason,
            install=resolved_install,
            baseline=baseline,
            after_fix=after_fix,
            edits=tuple(settled_edits),
            typecheck_baseline=tc_baseline,
            typecheck_after=tc_after,
            tier=ConfidenceTier.MEDIUM if not unattributed else ConfidenceTier.LOW,
            tier_reason=(
                f"`tsc --noEmit` attributed {len(added)} new diagnostic(s) to specific file(s)"
                if not unattributed
                else f"`tsc --noEmit` reported {len(added)} new diagnostic(s), "
                f"{len(unattributed)} outside any edited file"
            ),
        )

    def _decide_with_static_check(
        self,
        edits: list[FileEdit],
        on_disk: list[FileEdit],
        *,
        ran: bool,
        skipped_reason: str,
        install: InstallResult | None,
        baseline: TestRunResult | None,
        after_fix: TestRunResult | None,
        suspect,
    ) -> VerificationReport:
        """The non-TypeScript twin of the ``tsc --noEmit`` fallback.

        Same before/after, same identity diffing, same attribution rule: a
        static diagnostic names its own file, so blame is exact and only
        diagnostics landing outside every edited file are ambiguous.
        """
        root = self._editor.checkout.path
        touched = tuple(e.relpath for e in on_disk)
        edit_by_relpath = {e.relpath: e for e in on_disk}

        for relpath in touched:
            self._editor.revert(relpath)
        before = run_static_check(
            root,
            ecosystem=self._ecosystem,
            timeout=self._static_check_timeout,
            max_output_bytes=self._max_output_bytes,
        )
        for relpath in touched:
            self._editor.write_fix(relpath, edit_by_relpath[relpath].fixed_content)

        if not before.usable:
            return suspect(
                f"static check unavailable: {before.skipped_reason or 'baseline produced no usable output'}",
                resolved_install=install,
            )

        after = run_static_check(
            root,
            ecosystem=self._ecosystem,
            timeout=self._static_check_timeout,
            max_output_bytes=self._max_output_bytes,
        )
        if not after.usable:
            return suspect(
                f"static check unavailable: {after.skipped_reason or 'after-fix produced no usable output'}",
                resolved_install=install,
            )

        added = new_static_diagnostics(before, after)
        if not added:
            return VerificationReport(
                ran=ran,
                skipped_reason=skipped_reason,
                install=install,
                baseline=baseline,
                after_fix=after_fix,
                edits=tuple(self._settle(e, EditVerdict.KEPT) for e in edits),
                static_baseline=before,
                static_after=after,
                tier=ConfidenceTier.MEDIUM,
                tier_reason=f"`{after.tool}` introduced no new diagnostics",
            )

        blamed = {d.relpath for d in added} & set(touched)
        unattributed = [d for d in added if d.relpath not in touched]

        settled: list[FileEdit] = []
        for edit in edits:
            if not edit.is_on_disk:
                settled.append(edit)
            elif edit.relpath in blamed:
                self._editor.revert(edit.relpath)
                settled.append(self._settle(edit, EditVerdict.REVERTED))
            elif unattributed:
                settled.append(self._settle(edit, EditVerdict.SUSPECT))
            else:
                settled.append(self._settle(edit, EditVerdict.KEPT))

        return VerificationReport(
            ran=ran,
            skipped_reason=skipped_reason,
            install=install,
            baseline=baseline,
            after_fix=after_fix,
            edits=tuple(settled),
            static_baseline=before,
            static_after=after,
            tier=ConfidenceTier.MEDIUM if not unattributed else ConfidenceTier.LOW,
            tier_reason=(
                f"`{after.tool}` attributed {len(added)} new diagnostic(s) to specific file(s)"
                if not unattributed
                else f"`{after.tool}` reported {len(added)} new diagnostic(s), "
                f"{len(unattributed)} outside any edited file"
            ),
        )

    def _characterize_always(
        self, on_disk: list[FileEdit], install: InstallResult | None
    ) -> CharacterizationReport | None:
        """Always-attempt characterization wrapper.

        When ``characterization_always_run`` is True (default), attempts
        for every fix regardless of existing test coverage. When False,
        delegates to the original gated helper.
        """
        if not getattr(self, "characterization_always_run", True):
            return self._characterize_when_available(on_disk, install)
        # Same preconditions as the original — just skip the coverage-gap gate.
        if (
            self._characterizer is None
            or self._change is None
            or len(on_disk) != 1
            or install is None
            or not install.ok
        ):
            return None
        edit = on_disk[0]
        if not self._characterizer.supports(edit.relpath):
            return None
        return self._characterizer.characterize(
            root=self._editor.checkout.path,
            relpath=edit.relpath,
            original_source=edit.original_content,
            change=self._change,
            apply_fix=lambda: self._editor.write_fix(edit.relpath, edit.fixed_content),
            revert_fix=lambda: self._editor.revert(edit.relpath),
        )

    def _characterization_stage_result(self, report: CharacterizationReport | None) -> StageResult:
        """Convert a CharacterizationReport (or None) to a StageResult."""
        if report is None:
            # No report = stage couldn't run — structured skip.
            strict = getattr(self, "characterization_strict", False)
            status = StageStatus.FAILED if strict else StageStatus.SKIPPED
            return StageResult(
                stage="characterization",
                status=status,
                skip_reason="preconditions_not_met" if status == StageStatus.SKIPPED else None,
                detail=(
                    "characterizer unavailable, multiple files, or no install"
                    if status == StageStatus.SKIPPED
                    else "characterization_strict=True; preconditions not met"
                ),
            )
        if not report.ran:
            strict = getattr(self, "characterization_strict", False)
            status = StageStatus.FAILED if strict else StageStatus.SKIPPED
            return StageResult(
                stage="characterization",
                status=status,
                skip_reason=report.skipped_reason or "not_ran"
                if status == StageStatus.SKIPPED
                else None,
                detail=report.skipped_reason,
            )
        if report.confirmed:
            return StageResult(
                stage="characterization",
                status=StageStatus.PASSED,
                detail="fixture preserved pre-migration behaviour",
                duration_seconds=report.cost_usd,  # cost as a proxy; real duration not stored
            )
        return StageResult(
            stage="characterization",
            status=StageStatus.FAILED,
            detail=report.rejected_reason or "fixture failed",
        )

    def _characterize_when_available(
        self, on_disk: list[FileEdit], install: InstallResult | None
    ) -> CharacterizationReport | None:
        """Try the pre-fix characterization oracle as a last resort.

        It intentionally handles one touched CommonJS file at a time. A
        multi-file migration needs a multi-subject oracle and must remain
        suspect rather than pretending one file's fixture proved the batch.
        """
        if (
            self._characterizer is None
            or self._change is None
            or len(on_disk) != 1
            or install is None
            or not install.ok
        ):
            return None
        edit = on_disk[0]
        if not self._characterizer.supports(edit.relpath):
            return None
        return self._characterizer.characterize(
            root=self._editor.checkout.path,
            relpath=edit.relpath,
            original_source=edit.original_content,
            change=self._change,
            apply_fix=lambda: self._editor.write_fix(edit.relpath, edit.fixed_content),
            revert_fix=lambda: self._editor.revert(edit.relpath),
        )

    @staticmethod
    def _mark_suspect(edit: FileEdit) -> FileEdit:
        if not edit.is_on_disk:
            return edit
        return Verifier._settle(edit, EditVerdict.SUSPECT)

    @staticmethod
    def _settle(edit: FileEdit, verdict: EditVerdict) -> FileEdit:
        return dataclasses.replace(edit, verdict=verdict)
