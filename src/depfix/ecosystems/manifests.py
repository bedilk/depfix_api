"""Best-effort dependency-declaration scanning for every non-npm ecosystem.

npm stays in :mod:`depfix.scanners.manifest` (lockfile-resolution semantics
there are subtle and battle-tested); everything else lives here as one small
parser per ecosystem. Each parser answers exactly one question -- "which of
these SDK packages does this checkout declare, at what version" -- and is
deliberately tolerant: a manifest we can't parse contributes nothing rather
than failing the scan, because a scan that dies on one weird pom.xml never
gets to report the four other repos that matter.

Parsers take *text* wherever practical so tests exercise them with inline
fixtures instead of temp trees.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

import defusedxml.ElementTree as ElementTree

from depfix.ecosystems.base import DependencyDeclaration

logger = logging.getLogger(__name__)


def iter_manifest_files(root: Path, names: Iterable[str], skip_dirs: frozenset[str]) -> list[Path]:
    """Every manifest under ``root`` matching ``names`` (exact or ``*.`` glob),
    skipping vendored/build directories."""
    found: list[Path] = []
    for name in names:
        for path in sorted(root.rglob(name)):
            relative_parts = path.relative_to(root).parts[:-1]
            if any(part in skip_dirs for part in relative_parts):
                continue
            found.append(path)
    return found


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("skipping unreadable manifest %s: %s", path, exc)
        return None


def _read_toml(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.debug("skipping unparseable TOML %s: %s", path, exc)
        return None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("skipping unparseable JSON %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Python (PyPI)
# ---------------------------------------------------------------------------

#: ``openai==1.3.0`` / ``openai>=1,<2`` / ``openai`` / ``openai[datalib]~=1.0``
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>[=<>!~^].*)?$"
)


def _normalize_pypi(name: str) -> str:
    """PEP 503 normalization -- ``Google_Cloud.Storage`` == ``google-cloud-storage``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements_txt(text: str, wanted: set[str]) -> dict[str, str | None]:
    """``{normalized_name: version_spec}`` for every wanted requirement line."""
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        match = _REQUIREMENT_RE.match(line)
        if not match:
            continue
        name = _normalize_pypi(match.group("name"))
        if name in wanted:
            spec = (match.group("spec") or "").strip() or None
            out.setdefault(name, spec)
    return out


def _pyproject_requirements(data: dict) -> list[str]:
    reqs: list[str] = []
    project = data.get("project") or {}
    reqs.extend(r for r in project.get("dependencies", []) if isinstance(r, str))
    for group in (project.get("optional-dependencies") or {}).values():
        reqs.extend(r for r in group if isinstance(r, str))
    poetry_deps = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, constraint in poetry_deps.items():
        if name.lower() == "python":
            continue
        if isinstance(constraint, str):
            reqs.append(f"{name}{constraint if constraint[:1] in '=<>!~^' else '==' + constraint}")
        else:
            reqs.append(name)
    return reqs


def _python_locked_versions(root: Path, wanted: set[str]) -> dict[str, str]:
    """Exact versions from poetry.lock / uv.lock / Pipfile.lock, best-effort."""
    locked: dict[str, str] = {}
    for lock_name in ("poetry.lock", "uv.lock"):
        data = _read_toml(root / lock_name) if (root / lock_name).is_file() else None
        for pkg in (data or {}).get("package", []):
            name = _normalize_pypi(str(pkg.get("name", "")))
            if name in wanted and pkg.get("version"):
                locked.setdefault(name, str(pkg["version"]))
    pipfile_lock = _read_json(root / "Pipfile.lock") if (root / "Pipfile.lock").is_file() else None
    if pipfile_lock:
        for section in ("default", "develop"):
            for raw_name, entry in (pipfile_lock.get(section) or {}).items():
                name = _normalize_pypi(raw_name)
                version = entry.get("version") if isinstance(entry, dict) else None
                if name in wanted and isinstance(version, str):
                    locked.setdefault(name, version.lstrip("="))
    return locked


def scan_python_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    wanted = {_normalize_pypi(p) for p in packages}
    display = {_normalize_pypi(p): p for p in packages}
    declared: dict[str, tuple[str, str | None]] = {}  # name -> (manifest, spec)

    for req_path in iter_manifest_files(
        root, ["requirements.txt", "requirements-dev.txt"], frozenset({".git"})
    ):
        text = _read(req_path)
        if text is None:
            continue
        for name, spec in parse_requirements_txt(text, wanted).items():
            declared.setdefault(name, (str(req_path), spec))

    for pyproject_path in iter_manifest_files(root, ["pyproject.toml"], frozenset({".git"})):
        data = _read_toml(pyproject_path)
        if data is None:
            continue
        for req in _pyproject_requirements(data):
            match = _REQUIREMENT_RE.match(req)
            if not match:
                continue
            name = _normalize_pypi(match.group("name"))
            if name in wanted:
                declared.setdefault(
                    name, (str(pyproject_path), (match.group("spec") or "").strip() or None)
                )

    locked = _python_locked_versions(root, wanted)
    return [
        DependencyDeclaration(
            package=display[name],
            manifest_path=manifest,
            declared_version=spec,
            resolved_version=locked.get(name),
            source="lockfile" if name in locked else "manifest",
        )
        for name, (manifest, spec) in declared.items()
    ]


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

_GO_REQUIRE_RE = re.compile(
    r"^\s*(?:require\s+)?(?P<module>[\w./-]+)\s+(?P<version>v[\w.+-]+)", re.MULTILINE
)


def scan_go_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    out: list[DependencyDeclaration] = []
    for gomod in iter_manifest_files(root, ["go.mod"], frozenset({".git"})):
        text = _read(gomod)
        if text is None:
            continue
        for match in _GO_REQUIRE_RE.finditer(text):
            module = match.group("module")
            for wanted in packages:
                # go module paths version-suffix majors: stripe-go/v76 still
                # matches a provider-declared stripe-go.
                if (
                    module == wanted
                    or module.startswith(wanted.rstrip("/") + "/")
                    or wanted.startswith(module + "/")
                ):
                    out.append(
                        DependencyDeclaration(
                            package=wanted,
                            manifest_path=str(gomod),
                            declared_version=match.group("version"),
                            resolved_version=match.group("version"),  # go.mod pins exactly
                            source="manifest",
                        )
                    )
                    break
    return out


# ---------------------------------------------------------------------------
# Rust (crates.io)
# ---------------------------------------------------------------------------


def scan_rust_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    wanted = {p.replace("_", "-") for p in packages}
    display = {p.replace("_", "-"): p for p in packages}
    out: dict[str, DependencyDeclaration] = {}

    for cargo_toml in iter_manifest_files(root, ["Cargo.toml"], frozenset({".git", "target"})):
        data = _read_toml(cargo_toml)
        if data is None:
            continue
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            for name, constraint in (data.get(section) or {}).items():
                normalized = name.replace("_", "-")
                if normalized not in wanted:
                    continue
                version = (
                    constraint
                    if isinstance(constraint, str)
                    else (constraint.get("version") if isinstance(constraint, dict) else None)
                )
                out.setdefault(
                    normalized,
                    DependencyDeclaration(
                        package=display[normalized],
                        manifest_path=str(cargo_toml),
                        declared_version=version,
                        resolved_version=None,
                        source="manifest",
                    ),
                )

    cargo_lock = _read_toml(root / "Cargo.lock") if (root / "Cargo.lock").is_file() else None
    for pkg in (cargo_lock or {}).get("package", []):
        normalized = str(pkg.get("name", "")).replace("_", "-")
        if normalized in out and pkg.get("version"):
            entry = out[normalized]
            out[normalized] = DependencyDeclaration(
                package=entry.package,
                manifest_path=entry.manifest_path,
                declared_version=entry.declared_version,
                resolved_version=str(pkg["version"]),
                source="lockfile",
            )
    return list(out.values())


# ---------------------------------------------------------------------------
# Ruby (RubyGems)
# ---------------------------------------------------------------------------

_GEMFILE_RE = re.compile(
    r"""^\s*gem\s+['"](?P<name>[\w-]+)['"](?:\s*,\s*['"](?P<constraint>[^'"]+)['"])?""",
    re.MULTILINE,
)
_GEMFILE_LOCK_RE = re.compile(r"^\s{4}(?P<name>[\w-]+)\s+\((?P<version>[\w.]+)\)", re.MULTILINE)


def scan_ruby_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    wanted = set(packages)
    out: dict[str, DependencyDeclaration] = {}
    for gemfile in iter_manifest_files(root, ["Gemfile"], frozenset({".git", "vendor"})):
        text = _read(gemfile)
        if text is None:
            continue
        for match in _GEMFILE_RE.finditer(text):
            if match.group("name") in wanted:
                out.setdefault(
                    match.group("name"),
                    DependencyDeclaration(
                        package=match.group("name"),
                        manifest_path=str(gemfile),
                        declared_version=match.group("constraint"),
                        resolved_version=None,
                        source="manifest",
                    ),
                )
    lock_text = _read(root / "Gemfile.lock") if (root / "Gemfile.lock").is_file() else None
    for match in _GEMFILE_LOCK_RE.finditer(lock_text or ""):
        if match.group("name") in out:
            entry = out[match.group("name")]
            out[match.group("name")] = DependencyDeclaration(
                package=entry.package,
                manifest_path=entry.manifest_path,
                declared_version=entry.declared_version,
                resolved_version=match.group("version"),
                source="lockfile",
            )
    return list(out.values())


# ---------------------------------------------------------------------------
# PHP (Composer)
# ---------------------------------------------------------------------------


def scan_php_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    wanted = {p.lower() for p in packages}
    display = {p.lower(): p for p in packages}
    out: dict[str, DependencyDeclaration] = {}
    for composer in iter_manifest_files(root, ["composer.json"], frozenset({".git", "vendor"})):
        data = _read_json(composer)
        if data is None:
            continue
        for section in ("require", "require-dev"):
            for name, constraint in (data.get(section) or {}).items():
                if name.lower() in wanted:
                    out.setdefault(
                        name.lower(),
                        DependencyDeclaration(
                            package=display[name.lower()],
                            manifest_path=str(composer),
                            declared_version=str(constraint),
                            resolved_version=None,
                            source="manifest",
                        ),
                    )
    lock = _read_json(root / "composer.lock") if (root / "composer.lock").is_file() else None
    for section in ("packages", "packages-dev"):
        for pkg in (lock or {}).get(section, []):
            name = str(pkg.get("name", "")).lower()
            if name in out and pkg.get("version"):
                entry = out[name]
                out[name] = DependencyDeclaration(
                    package=entry.package,
                    manifest_path=entry.manifest_path,
                    declared_version=entry.declared_version,
                    resolved_version=str(pkg["version"]).lstrip("v"),
                    source="lockfile",
                )
    return list(out.values())


# ---------------------------------------------------------------------------
# Java / Kotlin (Maven coordinates, Maven + Gradle build files)
# ---------------------------------------------------------------------------

#: ``implementation "com.stripe:stripe-java:24.0.0"`` and kts variants.
_GRADLE_COORD_RE = re.compile(
    r"""['"](?P<group>[\w.-]+):(?P<artifact>[\w.-]+)(?::(?P<version>[\w.+-]+))?['"]"""
)


def _maven_key(package: str) -> tuple[str, str | None]:
    """providers.yaml spells a Java SDK as ``group:artifact`` (preferred) or
    a bare artifact id."""
    group, sep, artifact = package.partition(":")
    return (group, artifact) if sep else (package, None)


def scan_jvm_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    out: dict[str, DependencyDeclaration] = {}

    def note(package: str, manifest: Path, version: str | None) -> None:
        out.setdefault(
            package,
            DependencyDeclaration(
                package=package,
                manifest_path=str(manifest),
                declared_version=version,
                resolved_version=None,
                source="manifest",
            ),
        )

    for pom in iter_manifest_files(root, ["pom.xml"], frozenset({".git", "target"})):
        text = _read(pom)
        if text is None:
            continue
        try:
            tree = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            continue
        namespace = tree.tag.split("}")[0] + "}" if tree.tag.startswith("{") else ""
        for dep in tree.iter(f"{namespace}dependency"):
            group = dep.findtext(f"{namespace}groupId") or ""
            artifact = dep.findtext(f"{namespace}artifactId") or ""
            version = dep.findtext(f"{namespace}version")
            for package in packages:
                wanted_group, wanted_artifact = _maven_key(package)
                if (wanted_artifact and group == wanted_group and artifact == wanted_artifact) or (
                    not wanted_artifact and artifact == wanted_group
                ):
                    note(package, pom, version)

    for gradle in iter_manifest_files(
        root, ["build.gradle", "build.gradle.kts"], frozenset({".git", "build"})
    ):
        text = _read(gradle)
        if text is None:
            continue
        for match in _GRADLE_COORD_RE.finditer(text):
            for package in packages:
                wanted_group, wanted_artifact = _maven_key(package)
                if (
                    wanted_artifact
                    and match.group("group") == wanted_group
                    and match.group("artifact") == wanted_artifact
                ) or (not wanted_artifact and match.group("artifact") == wanted_group):
                    note(package, gradle, match.group("version"))
    return list(out.values())


# ---------------------------------------------------------------------------
# C# (NuGet)
# ---------------------------------------------------------------------------

#: Two-pass parse: match the whole tag, then pull attributes out of it --
#: a single regex with an optional trailing Version group silently never
#: matches the attribute (the lazy gap before an optional group is
#: satisfied by matching nothing).
_CSPROJ_TAG_RE = re.compile(r"<PackageReference\b[^>]*>")
_CSPROJ_INCLUDE_RE = re.compile(r'Include\s*=\s*"(?P<name>[\w.-]+)"')
_CSPROJ_VERSION_RE = re.compile(r'Version\s*=\s*"(?P<version>[\w.+-]+)"')


def scan_nuget_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    wanted = {p.lower() for p in packages}
    display = {p.lower(): p for p in packages}
    out: dict[str, DependencyDeclaration] = {}
    for csproj in iter_manifest_files(
        root,
        ["*.csproj", "packages.config", "Directory.Packages.props"],
        frozenset({".git", "bin", "obj"}),
    ):
        text = _read(csproj)
        if text is None:
            continue
        for tag_match in _CSPROJ_TAG_RE.finditer(text):
            tag = tag_match.group(0)
            include = _CSPROJ_INCLUDE_RE.search(tag)
            if include is None or include.group("name").lower() not in wanted:
                continue
            version = _CSPROJ_VERSION_RE.search(tag)
            out.setdefault(
                include.group("name").lower(),
                DependencyDeclaration(
                    package=display[include.group("name").lower()],
                    manifest_path=str(csproj),
                    declared_version=version.group("version") if version else None,
                    resolved_version=None,
                    source="manifest",
                ),
            )
    return list(out.values())


# ---------------------------------------------------------------------------
# Swift (SwiftPM)
# ---------------------------------------------------------------------------

_SWIFT_PACKAGE_RE = re.compile(
    r"""\.package\s*\(\s*url:\s*"(?P<url>[^"]+)"\s*,\s*(?:from:\s*"(?P<version>[\w.]+)"|exact:\s*"(?P<exact>[\w.]+)")?"""
)


def scan_swift_dependencies(root: Path, packages: list[str]) -> list[DependencyDeclaration]:
    out: dict[str, DependencyDeclaration] = {}
    for manifest in iter_manifest_files(root, ["Package.swift"], frozenset({".git", ".build"})):
        text = _read(manifest)
        if text is None:
            continue
        for match in _SWIFT_PACKAGE_RE.finditer(text):
            url_tail = match.group("url").rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            for package in packages:
                if url_tail.lower() == package.lower():
                    out.setdefault(
                        package,
                        DependencyDeclaration(
                            package=package,
                            manifest_path=str(manifest),
                            declared_version=match.group("version") or match.group("exact"),
                            resolved_version=None,
                            source="manifest",
                        ),
                    )
    resolved = (
        _read_json(root / "Package.resolved") if (root / "Package.resolved").is_file() else None
    )
    for pin in (resolved or {}).get("pins") or (resolved or {}).get("object", {}).get("pins") or []:
        identity = str(pin.get("identity") or pin.get("package") or "").lower()
        version = (pin.get("state") or {}).get("version")
        if identity in {p.lower() for p in out} and version:
            entry = out[next(p for p in out if p.lower() == identity)]
            out[entry.package] = DependencyDeclaration(
                package=entry.package,
                manifest_path=entry.manifest_path,
                declared_version=entry.declared_version,
                resolved_version=str(version),
                source="lockfile",
            )
    return list(out.values())
