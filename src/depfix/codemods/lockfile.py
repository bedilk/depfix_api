"""JavaScript dependency-source edits and lockfile refresh adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from depfix.core.models import BreakingChange
from depfix.scanners.manifest import declared_major

_OPERATOR_RE = re.compile(r"^([\^~>=<\s]*)")
_SEMVER = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:[-+][0-9A-Za-z.-]+)?$")
_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
_SKIP = {"node_modules", ".git", "dist", "build", "coverage", ".next"}


@dataclass(frozen=True)
class ManifestBump:
    manifest_relpath: str
    original_content: str
    fixed_content: str
    package: str
    section: str
    old_range: str
    new_range: str

    @property
    def note(self) -> str:
        return (
            f"{self.manifest_relpath}: bumped {self.package} {self.section} "
            f"{self.old_range!r} -> {self.new_range!r}; regenerate the lockfile before merging"
        )


@dataclass(frozen=True)
class DriftPlan:
    """Safe manifest edits for a drift update, plus explicit refusals."""

    bumps: list[ManifestBump]
    declines: list[str]
    cross_major: bool = False


def _major(value: str) -> int | None:
    # Stripe-style dated API versions are not semver package releases.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    return declared_major(value)


def _bump_range(value: str, major: int) -> str | None:
    raw = value.strip()
    if not raw or raw in {"*", "x", "latest"} or ":" in raw:
        return None
    # Extract operator prefix (^, ~, >=, etc.) and reconstruct with new major
    match = _OPERATOR_RE.match(raw)
    if match is None or declared_major(raw) is None:
        return None
    operator = match.group(1).strip() or "^"
    return f"{operator}{major}.0.0"


def _semver(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))  # type: ignore[return-value]


@dataclass(frozen=True)
class _Upgradable:
    """Whether one declaration may be rewritten, and why not when it can't."""

    current: tuple[int, int, int] | None = None
    decline: str = ""

    @property
    def ok(self) -> bool:
        return self.current is not None and not self.decline


def _declaration_upgradable(current_text: str, change: BreakingChange) -> _Upgradable:
    """Decide whether a declared range may be moved to the change's new version.

    Two valid upgrade scenarios:
    1. Declaration is on the SAME major as the drift's OLD side (standard
       same-major bump within the tracked range).
    2. Declaration is on the SAME major as the drift's NEW side but behind
       the new version (e.g. declared ``^11.10.0``, drift says ``12.16.0 →
       12.19.0`` — the repo already migrated to major 12 independently but
       is behind on minor/patch; a rescan is NOT needed, only a bump).
    """
    old = _semver(change.old_version)
    new = _semver(change.new_version)
    current = _semver(current_text.lstrip("^~<>= "))
    if new is None:
        return _Upgradable(decline=f"{change.new_version!r} is not plain semver")
    if current is None:
        return _Upgradable(
            decline=f"declared as {current_text!r}, which depfix will not rewrite automatically"
        )
    if old is not None and current[0] != old[0]:
        # The declared major doesn't match the drift record's OLD side.
        # But if it matches the NEW side and is behind, this is still a
        # valid same-major bump — the repo already crossed the major
        # boundary independently and just needs to catch up.
        if current[0] == new[0] and current < new:
            return _Upgradable(current=current)
        return _Upgradable(
            decline=(
                f"declared {current_text!r} (major {current[0]}), but this drift record is for "
                f"major {old[0]}; a rescan will produce the record for this workspace"
            )
        )
    if current >= new:
        return _Upgradable(decline="")
    return _Upgradable(current=current)


def dependency_drift_policy(
    change: BreakingChange,
    *,
    max_major_gap: int = 0,
    max_minor_gap: int | None = None,
    scope: str = "one-major",
) -> str:
    """Return the review policy for a registry version movement.

    ``scope`` controls which drift distances produce a PR artifact at all:

    * ``one-major`` (default): a same-major drift, or a single-major step
      (N -> N+1), is actionable; a gap of 2+ majors is ``declined``
      (no PR artifact). ``3.0.0 -> 4.0.0`` qualifies; ``3.1.2 -> 5.6.4`` does not.
    * ``any-minor``: only a same-major drift (any minor/patch distance) is
      actionable; ANY major step is ``declined``. ``3.4.2 -> 3.6.1``
      qualifies; ``2.1.2 -> 4.6.4`` does not.
    * ``any``: every forward move is actionable regardless of distance
      (``depfix scan --anyversion``).

    Within an actionable scope, ``max_major_gap`` / ``max_minor_gap`` decide
    ``automatic`` vs ``review_pr``.
    """
    old, new = _semver(change.old_version), _semver(change.new_version)
    if old is None or new is None or new <= old:
        return "declined"
    major_gap = new[0] - old[0]

    # --- scope gate: is this drift distance eligible for a PR at all? --------
    if scope == "any-minor":
        if major_gap != 0:
            return "declined"
    elif scope == "one-major" and major_gap > 1:
        return "declined"
    # scope == "any": every forward move is eligible; fall through.

    # --- actionable vs review within the eligible band -----------------------
    if major_gap > max_major_gap:
        return "review_pr"
    if major_gap == 0 and max_minor_gap is not None and (new[1] - old[1]) > max_minor_gap:
        return "review_pr"
    return "automatic"


def describe_drift(change: BreakingChange) -> str:
    """One line a reviewer can act on: how far this move actually travels."""
    old, new = _semver(change.old_version), _semver(change.new_version)
    if old is None or new is None:
        return f"{change.old_version} -> {change.new_version} (unparseable)"
    major_gap = new[0] - old[0]
    if major_gap == 0:
        return f"same major, {new[1] - old[1]} minor step(s)"
    step = "one major step" if major_gap == 1 else f"{major_gap} major versions"
    return f"{step} ({old[0]} -> {new[0]})"


def _replace_range_version(value: str, new_version: str) -> str | None:
    raw = value.strip()
    if not raw or raw in {"*", "x", "latest"} or ":" in raw:
        return None
    # Extract operator prefix and verify the range is parseable
    match = _OPERATOR_RE.match(raw)
    if match is None or declared_major(raw) is None:
        return None
    return f"{match.group(1)}{new_version}"


def build_dependency_drift_plan(
    root: Path, change: BreakingChange, *, scope: str = "one-major"
) -> DriftPlan:
    """Build npm edits for a verified drift or draft review PR.

    This is deliberately separate from ``build_manifest_bumps``: API
    migrations always move to a new major, whereas a registry drift preserves
    the declared range operator and targets only the observed old version.
    """
    policy = dependency_drift_policy(change, scope=scope)
    if policy == "declined":
        return DriftPlan([], [])
    new_version = _semver(change.new_version)
    if new_version is None:
        return DriftPlan([], [])
    catalog_plan = _build_pnpm_catalog_plan(root, change)
    if catalog_plan.bumps or catalog_plan.declines:
        return catalog_plan
    bumps: list[ManifestBump] = []
    declines: list[str] = []
    for path in root.rglob("package.json"):
        if any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            original = path.read_text(encoding="utf-8")
            document = json.loads(original)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        relpath = path.relative_to(root).as_posix()
        for section in _SECTIONS:
            dependencies = document.get(section)
            current = dependencies.get(change.package) if isinstance(dependencies, dict) else None
            if not isinstance(current, str):
                continue
            if current.startswith("catalog:"):
                catalog_ref = current.removeprefix("catalog:")
                label = f"catalog:{catalog_ref}" if catalog_ref else "catalog:"
                declines.append(
                    f"{relpath}: {change.package} is declared '{current}', which depfix "
                    f"will not rewrite automatically (edit pnpm-workspace.yaml's {label} "
                    "entry directly, or run with --provider to target the catalog)"
                )
                break
            verdict = _declaration_upgradable(current, change)
            if not verdict.ok:
                if verdict.decline:
                    declines.append(f"{relpath}: {verdict.decline}")
                break
            new_range = _replace_range_version(current, change.new_version)
            if new_range is None:
                declines.append(f"{relpath}: cannot rewrite the range {current!r}")
                break
            if policy == "automatic" and verdict.current[0] != new_version[0]:  # type: ignore[index]
                declines.append(
                    f"{relpath}: refusing to bump {change.package} {current!r} to "
                    f"{change.new_version} -- that crosses a major boundary"
                )
                break
            dependencies[change.package] = new_range  # type: ignore[index]
            indent = 4 if '\n    "' in original else 2
            fixed = json.dumps(document, indent=indent) + ("\n" if original.endswith("\n") else "")
            bumps.append(
                ManifestBump(relpath, original, fixed, change.package, section, current, new_range)
            )
            break
    if not bumps and not declines:
        declines.append(
            f"{change.package} is not declared in any package.json depfix scanned; "
            "the drift was detected via the registry feed but no manifest declares "
            "this package directly (it may be a transitive dependency, or the repo "
            "uses a non-standard package manager depfix doesn't support yet)"
        )
    return DriftPlan(bumps, declines)


def _build_pnpm_catalog_plan(
    root: Path,
    change: BreakingChange,
    *,
    target_version: str | None = None,
) -> DriftPlan:
    """Edit every pnpm catalog that owns ``change.package``."""
    workspace = root / "pnpm-workspace.yaml"
    catalog_names = _pnpm_catalog_consumers(root, change.package)
    if not workspace.is_file() or not catalog_names:
        return DriftPlan([], [])
    try:
        original = workspace.read_text(encoding="utf-8")
    except OSError:
        return DriftPlan([], [])

    fixed = original
    bumped: list[ManifestBump] = []
    declines: list[str] = []
    for catalog_name in sorted(catalog_names):
        label = "catalog" if not catalog_name else f"catalogs.{catalog_name}"
        replacement = _replace_pnpm_catalog_entry(
            fixed,
            change,
            catalog_name,
            target_version=target_version,
        )
        if isinstance(replacement, str):
            declines.append(f"pnpm-workspace.yaml ({label}): {replacement}")
            continue
        if replacement is None:
            continue
        prior, fixed, old_range, new_range = replacement
        bumped.append(
            ManifestBump(
                "pnpm-workspace.yaml",
                prior,
                fixed,
                change.package,
                label,
                old_range,
                new_range,
            )
        )
    if not bumped:
        return DriftPlan([], declines)
    return DriftPlan(
        [
            ManifestBump(
                "pnpm-workspace.yaml",
                original,
                fixed,
                change.package,
                "catalog",
                bumped[0].old_range,
                bumped[-1].new_range,
            )
        ],
        declines,
    )


def _build_pnpm_catalog_bumps(
    root: Path,
    change: BreakingChange,
    *,
    target_version: str | None = None,
) -> list[ManifestBump]:
    return _build_pnpm_catalog_plan(root, change, target_version=target_version).bumps


def _replace_pnpm_catalog_entry(
    original: str,
    change: BreakingChange,
    catalog_name: str,
    *,
    target_version: str | None = None,
) -> tuple[str, str, str, str] | str | None:
    """Replace one known pnpm catalog entry without reserializing YAML.

    Returns a 4-tuple on success, a decline-reason string on refusal, or
    ``None`` when the entry was not found in this catalog at all.
    """

    lines = original.splitlines(keepends=True)
    catalog_indent: int | None = None
    catalogs_indent: int | None = None
    package_pattern = re.compile(
        rf"^(?P<indent>\s*)(?P<quote>['\"]?){re.escape(change.package)}(?P=quote)"
        r"\s*:\s*(?P<value>[^#\r\n]+?)(?P<suffix>\s*(?:#.*)?(?:\r?\n)?)$"
    )
    for index, line in enumerate(lines):
        if catalog_indent is None:
            if catalog_name == "":
                match = re.match(r"^(?P<indent>\s*)catalog\s*:\s*(?:#.*)?(?:\r?\n)?$", line)
                if match:
                    catalog_indent = len(match.group("indent"))
                continue
            match = re.match(r"^(?P<indent>\s*)catalogs\s*:\s*(?:#.*)?(?:\r?\n)?$", line)
            if match:
                catalogs_indent = len(match.group("indent"))
            elif (
                catalogs_indent is not None
                and line.strip()
                and len(line) - len(line.lstrip()) <= catalogs_indent
            ):
                break
            elif catalogs_indent is not None:
                named = re.match(
                    rf"^(?P<indent>\s*){re.escape(catalog_name)}\s*:\s*(?:#.*)?(?:\r?\n)?$",
                    line,
                )
                if named and len(named.group("indent")) > catalogs_indent:
                    catalog_indent = len(named.group("indent"))
            continue
        if line.strip() and len(line) - len(line.lstrip()) <= catalog_indent:
            break
        match = package_pattern.match(line)
        if match is None or len(match.group("indent")) <= catalog_indent:
            continue
        current = match.group("value").strip()
        verdict = _declaration_upgradable(current, change)
        if not verdict.ok:
            return verdict.decline or None
        new_range = _replace_range_version(current, target_version or change.new_version)
        if new_range is None:
            return f"cannot rewrite the range {current!r}"
        lines[index] = (
            f"{match.group('indent')}{match.group('quote')}{change.package}"
            f"{match.group('quote')}: {new_range}{match.group('suffix')}"
        )
        fixed = "".join(lines)
        return original, fixed, current, new_range
    return None


def _pnpm_catalog_consumers(root: Path, package: str) -> set[str]:
    """Return catalog names that own *package* in this workspace.

    The empty string represents pnpm's default ``catalog:``. A consumer must
    name a catalog explicitly before its matching ``catalogs.<name>`` entry
    may be edited; this keeps unrelated named catalogs untouched.
    """
    names: set[str] = set()
    for path in root.rglob("package.json"):
        if any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for section in _SECTIONS:
            dependencies = document.get(section)
            if not isinstance(dependencies, dict):
                continue
            value = dependencies.get(package)
            if not isinstance(value, str) or not value.startswith("catalog:"):
                continue
            names.add(value.removeprefix("catalog:"))
    return names


def build_dependency_drift_bumps(root: Path, change: BreakingChange) -> list[ManifestBump]:
    """Retained compatibility seam for callers that only need writable edits."""
    return build_dependency_drift_plan(root, change).bumps


def build_manifest_bumps(root: Path, change: BreakingChange) -> list[ManifestBump]:
    """Build edits for manifests that pin the migrated package below its new major."""
    new_major = _major(change.new_version)
    if new_major is None:
        return []
    catalog_bumps = _build_pnpm_catalog_bumps(
        root,
        change,
        target_version=f"{new_major}.0.0",
    )
    if catalog_bumps:
        # A source migration in a pnpm workspace must still update the
        # workspace catalog, not the ``catalog:`` placeholder in consumers.
        return catalog_bumps
    bumps: list[ManifestBump] = []
    for path in root.rglob("package.json"):
        if any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            original = path.read_text(encoding="utf-8")
            document = json.loads(original)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        for section in _SECTIONS:
            dependencies = document.get(section)
            current = dependencies.get(change.package) if isinstance(dependencies, dict) else None
            if not isinstance(current, str):
                continue
            old_major = _major(current)
            new_range = _bump_range(current, new_major)
            if old_major is None or old_major >= new_major or new_range is None:
                # This section's pin isn't bumpable (unparseable, already
                # current, or no rewritable range) -- a *different* section
                # (e.g. peerDependencies vs dependencies) may still have a
                # bumpable pin for the same package, so keep looking rather
                # than abandoning the whole manifest.
                continue
            dependencies[change.package] = new_range  # type: ignore[index]
            indent = 4 if '\n    "' in original else 2
            fixed = json.dumps(document, indent=indent) + ("\n" if original.endswith("\n") else "")
            bumps.append(
                ManifestBump(
                    path.relative_to(root).as_posix(),
                    original,
                    fixed,
                    change.package,
                    section,
                    current,
                    new_range,
                )
            )
            break
    return bumps


def build_upgrade_manifest_bumps(root: Path, package: str, target_version: str) -> DriftPlan:
    """Rewrite every declaration of ``package`` to exactly ``target_version``.

    Unlike ``build_manifest_bumps`` (which floors to ``^{major}.0.0``), this
    targets the version the feed actually observed, so the manifest, the
    lockfile and the PR title all name the same release.
    """
    from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
    from depfix.sources.semver import parse_semver as _semver

    if _semver(target_version) is None:
        return DriftPlan([], [f"{target_version!r} is not plain semver"])

    # Re-use the existing drift path with a synthetic change pointing at the target
    synthetic = BreakingChange(
        package=package,
        old_version="0.0.0",
        new_version=target_version,
        old_api=f"{package}@0.0.0",
        new_api=f"{package}@{target_version}",
        description="upgrade",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
    )
    # Try pnpm catalog first
    catalog = _build_pnpm_catalog_plan(root, synthetic, target_version=target_version)
    if catalog.bumps or catalog.declines:
        return catalog

    bumps, declines = [], []
    for path in root.rglob("package.json"):
        if any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            original = path.read_text(encoding="utf-8")
            document = json.loads(original)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        relpath = path.relative_to(root).as_posix()
        for section in _SECTIONS:
            deps = document.get(section)
            current = deps.get(package) if isinstance(deps, dict) else None
            if not isinstance(current, str):
                continue
            if current.startswith("catalog:"):
                declines.append(f"{relpath}: {package} is '{current}'; edit the catalog")
                break
            new_range = _replace_range_version(current, target_version)
            if new_range is None:
                declines.append(f"{relpath}: cannot rewrite the range {current!r}")
                break
            deps[package] = new_range  # type: ignore[index]
            indent = 4 if '\n    "' in original else 2
            fixed = json.dumps(document, indent=indent) + ("\n" if original.endswith("\n") else "")
            bumps.append(
                ManifestBump(relpath, original, fixed, package, section, current, new_range)
            )
            break
    return DriftPlan(bumps, declines)


def try_refresh_javascript_lockfile(
    root: Path,
    *,
    timeout: float = 120.0,
    max_output_bytes: int = 2_000_000,
) -> tuple[bool, str]:
    """Refresh the lockfile owned by a JavaScript workspace.

    This is deliberately package-manager aware: ``package.json`` and pnpm
    catalog edits both change source-of-truth declarations, while lockfiles
    are generated output. The operation runs only in a disposable checkout,
    uses the repository's declared/cached runtime, and disables lifecycle
    scripts.
    """
    from depfix.scanners.workspace import find_workspace_root
    from depfix.verify.manager import prepare_package_manager
    from depfix.verify.sandbox import run_sandboxed

    workspace = find_workspace_root(root)
    prepared = prepare_package_manager(workspace, timeout=min(timeout, 90.0))
    if not prepared.ready:
        return False, prepared.detail or "JavaScript package-manager runtime unavailable"
    manager = prepared.package_manager
    executable = str(prepared.executable) if prepared.installed else manager
    if manager == "pnpm":
        argv = [executable, "install", "--lockfile-only", "--ignore-scripts"]
    elif manager == "npm":
        argv = [executable, "install", "--package-lock-only", "--ignore-scripts"]
    elif manager == "yarn":
        argv = [executable, "install", "--ignore-scripts"]
        if _manager_major(prepared.version) >= 2:
            argv.append("--mode=update-lockfile")
    elif manager == "bun":
        argv = [executable, "install", "--lockfile-only", "--ignore-scripts"]
    else:
        return False, f"unsupported JavaScript package manager: {manager}"
    result = run_sandboxed(argv, cwd=workspace, timeout=timeout, max_output_bytes=max_output_bytes)
    if not result.ok:
        message = result.error or result.stderr.strip() or f"exit {result.exit_code}"
        return False, f"{manager} lockfile refresh failed: {message[:200]}"
    return True, f"lockfile refreshed via `{' '.join(argv)}`"


def _manager_major(version: str) -> int:
    try:
        return int(version.split(".", 1)[0])
    except ValueError:
        return 0


def try_regenerate_lockfile(
    root: Path,
    *,
    timeout: float = 120.0,
    ignore_scripts: bool = True,
    max_output_bytes: int = 2_000_000,
) -> tuple[bool, str]:
    """Compatibility entry point for callers that need a JS lock refresh.

    ``ignore_scripts`` remains part of the interface for older callers; all
    supported refresh commands already disable lifecycle scripts.
    """
    del ignore_scripts
    return try_refresh_javascript_lockfile(
        root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )


def try_refresh_pnpm_lockfile(
    root: Path,
    *,
    timeout: float = 120.0,
    max_output_bytes: int = 2_000_000,
) -> tuple[bool, str]:
    """Backward-compatible alias for the package-manager-aware refresher."""
    return try_refresh_javascript_lockfile(
        root,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )


def lockfile_paths(root: Path) -> list[Path]:
    names = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"}
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name in names and "node_modules" not in path.parts
    ]
