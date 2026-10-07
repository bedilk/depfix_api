"""Conservative PyPI dependency-drift manifest edits.

The npm writer lives in :mod:`depfix.codemods.lockfile`; this adapter owns
Python's distinct requirement and TOML syntax.  It only rewrites a named,
versioned dependency and deliberately leaves lockfile refresh to the Python
package-manager adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from depfix.codemods.lockfile import dependency_drift_policy
from depfix.core.models import BreakingChange
from depfix.ecosystems.manifests import _normalize_pypi

_SKIP = frozenset(
    {"node_modules", ".git", "dist", "build", ".venv", "venv", ".depfix-venv", "__pycache__"}
)
_REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")
_SPECIFIER = r"(?:===|==|~=|>=|<=|!=|<|>)"
_REQ_LINE_RE = re.compile(
    rf"^(?P<prefix>\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?)"
    rf"\s*(?P<spec>{_SPECIFIER}[^;#\r\n]*)(?P<marker>\s*;[^#\r\n]*)?"
    r"(?P<comment>\s*#.*)?(?P<newline>\r?\n)?$"
)
_VERSION_IN_SPEC_RE = re.compile(r"(\d+)(?:\.\d+)*")


def _spec_major(spec: str) -> int | None:
    """Return the first major version mentioned in a requirement specifier."""
    match = _VERSION_IN_SPEC_RE.search(spec)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class PypiManifestBump:
    """One minimal textual change to a Python dependency declaration."""

    manifest_relpath: str
    original_content: str
    fixed_content: str
    package: str
    old_spec: str
    new_spec: str

    @property
    def note(self) -> str:
        return (
            f"{self.manifest_relpath}: bumped {self.package} {self.old_spec!r} -> "
            f"{self.new_spec!r}; regenerate the lockfile before merging"
        )


def _iter_files(root: Path, names: tuple[str, ...]) -> list[Path]:
    return [
        path
        for name in names
        for path in sorted(root.rglob(name))
        if not any(part in _SKIP for part in path.relative_to(root).parts[:-1])
    ]


def _bump_requirements_text(text: str, package: str, new_version: str) -> tuple[str, str | None]:
    """Rewrite matching requirement lines while retaining markers/comments."""
    wanted = _normalize_pypi(package)
    old_spec: str | None = None
    rewritten: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _REQ_LINE_RE.match(line)
        if match is None or _normalize_pypi(match.group("name")) != wanted:
            rewritten.append(line)
            continue
        matched_spec = match.group("spec")
        old_spec = old_spec or matched_spec.strip()
        separator = matched_spec[len(matched_spec.rstrip()) :]
        rewritten.append(
            f"{match.group('prefix')}=={new_version}{separator}{match.group('marker') or ''}"
            f"{match.group('comment') or ''}{match.group('newline') or ''}"
        )
    return "".join(rewritten), old_spec


def build_pypi_dependency_drift_bumps(root: Path, change: BreakingChange) -> list[PypiManifestBump]:
    """Return safe requirement/pyproject edits for a same-major PyPI drift."""
    if dependency_drift_policy(change) != "automatic":
        return []
    new_version = change.new_version.strip().lstrip("v")
    if not new_version:
        return []

    bumps: list[PypiManifestBump] = []
    for path in _iter_files(root, _REQUIREMENT_FILES):
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fixed, old_spec = _bump_requirements_text(original, change.package, new_version)
        if (
            old_spec is not None
            and _spec_major(old_spec) is not None
            and _spec_major(new_version) is not None
            and _spec_major(old_spec) != _spec_major(new_version)
        ):
            continue
        if old_spec is not None and fixed != original:
            bumps.append(
                PypiManifestBump(
                    path.relative_to(root).as_posix(),
                    original,
                    fixed,
                    change.package,
                    old_spec,
                    f"=={new_version}",
                )
            )

    for path in _iter_files(root, ("pyproject.toml",)):
        bump = _bump_pyproject(root, path, change.package, new_version)
        if bump is not None:
            bumps.append(bump)
    return bumps


def _bump_pyproject(
    root: Path, path: Path, package: str, new_version: str
) -> PypiManifestBump | None:
    """Make a minimal text edit rather than reserializing a TOML document."""
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    wanted = _normalize_pypi(package)
    old_spec: str | None = None

    # PEP 621 dependency list entry, including optional extras and marker.
    list_entry_re = re.compile(
        rf"(?P<quote>['\"])(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
        rf"(?P<extras>\[[^\]]*\])?(?P<spec>\s*{_SPECIFIER}[^'\";\r\n]*)"
        rf"(?P<marker>\s*;[^'\"\r\n]*)?(?P=quote)"
    )
    # Poetry's dependency tables use `name = "constraint"`.
    poetry_re = re.compile(
        r"^(?P<lead>\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*)"
        r"(?P<quote>['\"])(?P<body>[^'\"\r\n]+)(?P=quote)",
        re.MULTILINE,
    )

    def replace_list_entry(match: re.Match[str]) -> str:
        nonlocal old_spec
        if _normalize_pypi(match.group("name")) != wanted:
            return match.group(0)
        old_spec = old_spec or match.group("spec").strip()
        return (
            f"{match.group('quote')}{match.group('name')}{match.group('extras') or ''}"
            f"=={new_version}{match.group('marker') or ''}{match.group('quote')}"
        )

    def replace_poetry(match: re.Match[str]) -> str:
        nonlocal old_spec
        if _normalize_pypi(match.group("name")) != wanted:
            return match.group(0)
        old_spec = old_spec or match.group("body").strip()
        return f"{match.group('lead')}{match.group('quote')}=={new_version}{match.group('quote')}"

    fixed = list_entry_re.sub(replace_list_entry, original)
    fixed = poetry_re.sub(replace_poetry, fixed)
    if old_spec is None or fixed == original:
        return None
    if (
        _spec_major(old_spec) is not None
        and _spec_major(new_version) is not None
        and _spec_major(old_spec) != _spec_major(new_version)
    ):
        return None
    return PypiManifestBump(
        path.relative_to(root).as_posix(),
        original,
        fixed,
        package,
        old_spec,
        f"=={new_version}",
    )
