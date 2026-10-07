"""JavaScript manifest + lockfile scanning.

Resolves what version of a tracked SDK package a repo actually has, so the
call-site scanner can report "this repo's lockfile resolves openai to 4.1.0
but the call sites below still look like the v3 API" instead of guessing
from source code alone.

**Lockfile-first.** ``package-lock.json`` carries the *resolved* version --
what actually got installed -- which is the only thing that answers "is
this repo affected" with certainty. A semver range in ``package.json``
(``^3.0.0``) is a three-valued (yes/no/unknown) fallback when there is no
lockfile to resolve against.

``package-lock.json`` and ``pnpm-lock.yaml`` are supported. Workspace
manifests inherit a lockfile from the nearest parent directory, which lets a
``catalog:`` declaration be resolved from the root pnpm lockfile.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "coverage", ".next", "__pycache__"}


@dataclass(frozen=True)
class DependencyMatch:
    """One tracked SDK package found in one manifest/lockfile pair."""

    package: str
    manifest_path: str
    declared_range: str | None  # what package.json says, e.g. "^4.0.0"
    resolved_version: str | None  # what the lockfile actually resolved to, if present
    source: str  # "lockfile" | "range"


@dataclass
class ManifestScan:
    """Everything found scanning one checkout's manifests for tracked SDKs."""

    manifests_found: list[str] = field(default_factory=list)
    matches: list[DependencyMatch] = field(default_factory=list)


def find_manifests(root: Path) -> list[Path]:
    """Find every ``package.json`` under ``root``.

    Monorepo-aware (walks workspace subdirectories) but skips
    ``node_modules`` and friends -- a vendored copy of the SDK inside
    ``node_modules`` is not "this repo declaring a dependency".
    """
    found: list[Path] = []
    for path in root.rglob("package.json"):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        found.append(path)
    return sorted(found)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to parse %s: %s", path, exc)
        return None


def _load_yaml(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
        # pnpm v9 lockfiles use multi-document YAML (``---`` separator).
        # The second document typically carries the full ``packages`` map
        # needed for version resolution. Merge all dict documents so that
        # the richest ``packages`` section wins.
        merged: dict | None = None
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            if merged is None:
                merged = doc
                continue
            for key, value in doc.items():
                existing = merged.get(key)
                if (
                    isinstance(existing, dict)
                    and isinstance(value, dict)
                    and len(value) > len(existing)
                ) or key not in merged:
                    merged[key] = value
        return merged
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("failed to parse %s: %s", path, exc)
        return None


def _declared_ranges(manifest: dict) -> dict[str, str]:
    ranges: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = manifest.get(key) or {}
        for name, spec in section.items():
            if isinstance(spec, str):
                ranges.setdefault(name, spec)
    return ranges


def _resolve_from_lockfile_v1(lock: dict, package: str) -> str | None:
    """v1 lockfiles nest resolved deps recursively under ``dependencies``."""

    def _walk(deps: dict) -> str | None:
        if package in deps:
            version = deps[package].get("version")
            if version:
                return str(version)
        for entry in deps.values():
            nested = entry.get("dependencies")
            if nested:
                found = _walk(nested)
                if found:
                    return found
        return None

    return _walk(lock.get("dependencies") or {})


def _resolve_from_lockfile_v2plus(lock: dict, package: str) -> str | None:
    """v2/v3 lockfiles flatten every install into ``packages``, keyed by a
    ``node_modules`` path (``node_modules/foo``, or a nested workspace
    path like ``packages/api/node_modules/foo``)."""
    packages = lock.get("packages") or {}
    candidates = [
        key
        for key in packages
        if key == f"node_modules/{package}" or key.endswith(f"/node_modules/{package}")
    ]
    # Prefer the shallowest (most top-level) install for a stable answer
    # when the same package is hoisted at more than one depth.
    candidates.sort(key=lambda k: k.count("node_modules"))
    for key in candidates:
        version = packages[key].get("version")
        if version:
            return str(version)
    return None


def _resolve_from_lockfile(lock: dict, package: str) -> str | None:
    lockfile_version = int(lock.get("lockfileVersion", 1) or 1)
    if lockfile_version >= 2:
        resolved = _resolve_from_lockfile_v2plus(lock, package)
        if resolved:
            return resolved
    return _resolve_from_lockfile_v1(lock, package)


def _version_from_package_key(key: str, package: str) -> str | None:
    """Extract a package version from a pnpm package-key without guessing.

    pnpm v6 keys have a leading slash: ``/@sentry/node@10.70.0``.
    """
    prefix = f"{package}@"
    stripped = key.lstrip("/")
    if not stripped.startswith(prefix):
        return None
    candidate = stripped.removeprefix(prefix).split("(", 1)[0]
    return candidate if _SEMVER_RE.match(candidate) else None


def _resolve_from_pnpm_importers(lock: dict, package: str) -> str | None:
    """Resolve from per-importer records.

    pnpm writes ``specifier: catalog:sentry`` next to ``version: 10.70.0``,
    so this is the only place a ``catalog:`` dependency's real version is
    recoverable -- ``packages`` keys never mention the indirection.
    """
    best: tuple[int, int, int] | None = None
    found: str | None = None
    for importer in (lock.get("importers") or {}).values():
        if not isinstance(importer, dict):
            continue
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            entry = (importer.get(section) or {}).get(package)
            version = entry.get("version") if isinstance(entry, dict) else None
            if not isinstance(version, str):
                continue
            bare = version.split("(", 1)[0]
            parsed = _parse_semver(bare)
            if parsed is not None and (best is None or parsed < best):
                best, found = parsed, bare
    return found


def _resolve_from_pnpm_lockfile(lock: dict, package: str) -> str | None:
    """Resolve a package from pnpm v6--v9 ``packages`` entries.

    pnpm stores the real version in keys such as
    ``@prisma/client@6.14.0(prisma@6.14.0)``.  Prefer the lowest nesting-free
    candidate deterministically rather than deriving a version from a
    workspace ``catalog:`` declaration.
    """
    candidates: list[str] = []
    for key, entry in (lock.get("packages") or {}).items():
        if not isinstance(key, str):
            continue
        version = _version_from_package_key(key, package)
        if version:
            candidates.append(version)
        elif key == package and isinstance(entry, dict) and isinstance(entry.get("version"), str):
            # Older lockfile forms can keep a package version in the value.
            candidates.append(entry["version"])
    parsed = [(value, _parse_semver(value)) for value in candidates]
    known = [(value, version) for value, version in parsed if version is not None]
    return min(known, key=lambda item: item[1])[0] if known else None


def _parse_yarn_lockfile(text: str) -> dict[str, list[tuple[str, str]]]:
    """``{package: [(specifier, version), ...]}`` for every entry in a yarn.lock.

    One line-oriented pass rather than a regex per format: a top-level entry
    is an unindented line ending in ``:``, whose key lists one or more
    ``name@specifier`` pairs (v1 quotes only when it must, Berry always
    quotes, both may group with commas); ``version`` is read from the
    indented block beneath it. Known gap: a range containing a literal comma
    would be split -- Yarn writes ``>=1 <2`` and ``^1 || ^2``, neither of
    which does.
    """
    entries: dict[str, list[tuple[str, str]]] = {}
    pending: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            pending = []
            header = line.rstrip()
            if not header.endswith(":"):
                continue
            for token in header[:-1].split(","):
                name, _, specifier = token.strip().strip('"').rpartition("@")
                if name:
                    pending.append((name, specifier))
            continue
        if not pending:
            continue
        stripped = line.strip()
        if stripped.startswith("version"):
            version = stripped.removeprefix("version").lstrip(": ").strip().strip('"')
            for name, specifier in pending:
                entries.setdefault(name, []).append((specifier, version))
            pending = []
    return entries


def _resolve_from_yarn(
    entries: dict[str, list[tuple[str, str]]], package: str, declared_range: str | None
) -> str | None:
    """The version *this manifest's own range* resolved to, not the lowest in
    the workspace -- a monorepo holding two majors of one SDK must not report
    the major the scanned package isn't on."""
    candidates = entries.get(package) or []
    for specifier, version in candidates:
        if declared_range and specifier in (declared_range, f"npm:{declared_range}"):
            return version
    known = [(v, _parse_semver(v)) for _, v in candidates]
    resolved = [(v, s) for v, s in known if s is not None]
    return min(resolved, key=lambda item: item[1])[0] if resolved else None


def _nearest_lockfile(manifest_path: Path, root: Path, filename: str) -> Path | None:
    """Find a sibling or workspace-root lockfile without escaping *root*."""
    current = manifest_path.parent
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current == root:
            return None
        current = current.parent


def scan_manifests(root: Path, sdk_packages: list[str]) -> ManifestScan:
    """Scan every ``package.json`` under ``root`` for ``sdk_packages``,
    resolving actual installed versions via the nearest workspace lockfile
    when one exists."""
    scan = ManifestScan()
    for manifest_path in find_manifests(root):
        manifest = _load_json(manifest_path)
        if manifest is None:
            continue
        scan.manifests_found.append(str(manifest_path))

        ranges = _declared_ranges(manifest)
        wanted = [pkg for pkg in sdk_packages if pkg in ranges]
        if not wanted:
            continue

        npm_lock_path = _nearest_lockfile(manifest_path, root, "package-lock.json")
        pnpm_lock_path = _nearest_lockfile(manifest_path, root, "pnpm-lock.yaml")
        yarn_lock_path = _nearest_lockfile(manifest_path, root, "yarn.lock")
        npm_lock = _load_json(npm_lock_path) if npm_lock_path else None
        pnpm_lock = _load_yaml(pnpm_lock_path) if pnpm_lock_path else None
        yarn_entries: dict[str, list[tuple[str, str]]] | None = None
        if yarn_lock_path and yarn_lock_path.is_file():
            try:
                yarn_entries = _parse_yarn_lockfile(yarn_lock_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("failed to parse %s: %s", yarn_lock_path, exc)

        for package in wanted:
            resolved = _resolve_from_lockfile(npm_lock, package) if npm_lock else None
            if resolved is None and pnpm_lock is not None:
                # Try importers first -- the only place a catalog: reference resolves
                resolved = _resolve_from_pnpm_importers(pnpm_lock, package)
                if resolved is None:
                    resolved = _resolve_from_pnpm_lockfile(pnpm_lock, package)
            if resolved is None and yarn_entries is not None:
                resolved = _resolve_from_yarn(yarn_entries, package, ranges.get(package))
            scan.matches.append(
                DependencyMatch(
                    package=package,
                    manifest_path=str(manifest_path),
                    declared_range=ranges[package],
                    resolved_version=resolved,
                    source="lockfile" if resolved else "range",
                )
            )
    return scan


_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse_semver(text: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def affects_version(resolved_version: str, old_version: str, new_version: str) -> bool | None:
    """Is ``resolved_version`` on the "old", pre-breaking-change side of the
    ``old_version -> new_version`` boundary?

    Returns ``None`` (unknown) rather than guessing when any of the inputs
    isn't parseable as plain semver -- silently guessing "no" would hide a
    real match, and guessing "yes" would spam call sites in unaffected
    repos.
    """
    resolved = _parse_semver(resolved_version)
    new = _parse_semver(new_version)
    if resolved is None or new is None:
        return None
    return resolved < new


_DECLARED_MAJOR_RE = re.compile(r"^[~^>=<\s]*v?(\d+)")


def declared_major(range_spec: str) -> int | None:
    """Extract the major version from a semver range specification.

    Handles range operators: ^3.0.0 → 3, ~1.2.3 → 1, >=2.0.0 → 2, ^0.3.17 → 0.
    Returns None for non-numeric ranges (*, latest, git URLs, etc.).
    """
    spec = range_spec.strip()
    if not spec or spec in {"*", "latest", "x"}:
        return None
    if spec.startswith(("git+", "http://", "https://", "file:", "workspace:", "link:")):
        return None
    match = _DECLARED_MAJOR_RE.match(spec)
    return int(match.group(1)) if match else None


def range_allows_major(range_spec: str, major: int) -> bool | None:
    """Could this semver *range* (not a resolved version) ever resolve to
    the given major version?

    Three-valued on purpose: a declared range like ``^3.0.0`` clearly
    cannot resolve to major ``4`` (``False``); ``>=3.0.0`` clearly can
    (``True``); ``latest`` / ``*`` / a git URL / workspace protocol is
    ``None`` -- "can't tell from the range alone, check the lockfile".
    """
    spec = range_spec.strip()
    if not spec or spec in {"*", "latest", "x"}:
        return None
    if spec.startswith(("git+", "http://", "https://", "file:", "workspace:", "link:")):
        return None

    if "||" in spec:
        results = [range_allows_major(part, major) for part in spec.split("||")]
        if any(r is True for r in results):
            return True
        if all(r is False for r in results):
            return False
        return None

    if " - " in spec:
        low, high = (p.strip() for p in spec.split(" - ", 1))
        low_v, high_v = _parse_semver(low), _parse_semver(high)
        if low_v is None or high_v is None:
            return None
        return low_v[0] <= major <= high_v[0]

    parts = spec.split()
    if len(parts) > 1:
        # Multiple space-separated constraints in one range spec -- too
        # ambiguous to reason about safely; defer to the lockfile.
        return None

    match = re.match(r"^([~^>=<]*)(\d+)", parts[0] if parts else spec)
    if not match:
        return None
    operator, base_major = match.group(1), int(match.group(2))

    if operator in ("", "=", "^", "~"):
        # ^ and ~ both pin the major for a >=1.0.0 base, which covers every
        # SDK package this scanner cares about.
        return major == base_major
    if operator in (">=", ">"):
        return major >= base_major
    if operator in ("<=", "<"):
        return major <= base_major
    return None
