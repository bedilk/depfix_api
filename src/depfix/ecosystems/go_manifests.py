"""Conservative ``go.mod`` dependency-drift edits.

npm lives in :mod:`depfix.codemods.lockfile`, PyPI in
:mod:`depfix.ecosystems.pypi_manifests`, RubyGems in
:mod:`depfix.ecosystems.ruby_manifests`. Go gets its own adapter because two
of its rules have no analogue in any of them:

* **The module path carries the major version.** ``v2`` and later live at a
  *different import path* (``github.com/x/y/v2``), so a cross-major bump is a
  path change plus an import rewrite across every call site -- not a version
  token rewrite. This writer refuses anything that is not a same-major move
  and lets the drift be reported for review instead. Silently rewriting the
  token would produce a ``go.mod`` that cannot resolve.
* **``require`` has two syntaxes and shares the file with directives that
  look almost identical.** ``replace``/``exclude`` lines also name a module
  and a version; rewriting one of those would change which module is
  substituted, not which version is required. So this is a line-oriented
  parse that tracks whether it is inside a ``require ( ... )`` block, rather
  than a regex sweep over the whole file.

``go.mod`` is the source of truth; ``go.sum`` is generated output and is
refreshed afterwards by :mod:`depfix.ecosystems.go_lockfile`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from depfix.codemods.lockfile import dependency_drift_policy
from depfix.core.models import BreakingChange
from depfix.sources.semver import parse_semver

_SKIP_DIRS = frozenset({"vendor", ".git", "node_modules", "testdata", ".idea"})

#: ``[require ]<module> <version>[ // comment]`` -- the only shape this writer
#: will touch. ``lead`` preserves indentation (tabs in a grouped block) and an
#: optional inline ``require`` keyword; ``rest`` preserves ``// indirect``.
_MODULE_VERSION_RE = re.compile(
    r"^(?P<lead>\s*(?:require\s+)?)(?P<module>[^\s]+)\s+(?P<version>v[0-9][^\s]*)"
    r"(?P<rest>\s*(?://.*)?)$"
)
_REQUIRE_BLOCK_OPEN_RE = re.compile(r"^require\s*\($")
_MAJOR_SUFFIX_RE = re.compile(r"v\d+")


@dataclass(frozen=True)
class GoModuleBump:
    """One minimal textual change to a ``go.mod`` require directive."""

    manifest_relpath: str
    original_content: str
    fixed_content: str
    package: str
    old_version: str
    new_version: str

    @property
    def note(self) -> str:
        return (
            f"{self.manifest_relpath}: bumped {self.package} {self.old_version} -> "
            f"{self.new_version}; go.sum is refreshed from this edit before verification"
        )


def go_module_files(root: Path) -> list[Path]:
    """Every ``go.mod`` under ``root``, skipping vendored trees."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if "go.mod" in filenames:
            found.append(Path(dirpath) / "go.mod")
    return sorted(found)


def declares_module(root: Path, package: str) -> bool:
    """Whether any ``go.mod`` under ``root`` requires ``package``."""
    return any(
        _find_required_version(_read(path) or "", package) is not None
        for path in go_module_files(root)
    )


def build_go_dependency_drift_bumps(root: Path, change: BreakingChange) -> list[GoModuleBump]:
    """Safe ``go.mod`` edits for a same-major Go module drift."""
    if dependency_drift_policy(change) != "automatic":
        return []
    new_version = _normalise_version(change.new_version)
    new = parse_semver(new_version)
    if new is None:
        return []

    bumps: list[GoModuleBump] = []
    for path in go_module_files(root):
        original = _read(path)
        if original is None:
            continue
        fixed, old_version = _rewrite(original, change.package, new_version)
        if old_version is None or fixed == original:
            continue
        old = parse_semver(old_version)
        # A major move changes the module path, which is a different (and much
        # larger) edit than this writer is allowed to make.
        if old is None or old[0] != new[0]:
            continue
        bumps.append(
            GoModuleBump(
                manifest_relpath=path.relative_to(root).as_posix(),
                original_content=original,
                fixed_content=fixed,
                package=change.package,
                old_version=old_version,
                new_version=new_version,
            )
        )
    return bumps


# -- internals -----------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _normalise_version(raw: str) -> str:
    """Go versions are ``v``-prefixed; a feed may report the bare semver."""
    candidate = raw.strip()
    return candidate if candidate.startswith("v") else f"v{candidate}"


def _same_module(declared: str, wanted: str) -> bool:
    """Match a declared module path against a configured package name.

    Either side may carry the ``/vN`` major suffix: providers.yaml may name
    ``github.com/stripe/stripe-go`` while ``go.mod`` requires
    ``github.com/stripe/stripe-go/v76``. Only a *version* suffix is accepted,
    so a genuinely separate submodule (``.../client``) never matches.
    """
    if declared == wanted:
        return True
    for base, other in ((declared, wanted), (wanted, declared)):
        if other.startswith(f"{base}/") and _MAJOR_SUFFIX_RE.fullmatch(other[len(base) + 1 :]):
            return True
    return False


def _iter_require_lines(text: str):
    """Yield ``(index, line, newline, lines)`` for every require directive line.

    Skips ``replace``/``exclude`` and anything containing ``=>``: those name a
    module and a version too, and rewriting one would change substitution
    rather than requirement.
    """
    lines = text.splitlines(keepends=True)
    in_block = False
    for index, raw in enumerate(lines):
        line = raw.rstrip("\n").rstrip("\r")
        newline = raw[len(line) :]
        stripped = line.strip()
        if in_block:
            if stripped.startswith(")"):
                in_block = False
                continue
        elif _REQUIRE_BLOCK_OPEN_RE.match(stripped):
            in_block = True
            continue
        elif not stripped.startswith("require "):
            continue
        if "=>" in stripped:
            continue
        yield index, line, newline, lines


def _find_required_version(text: str, package: str) -> str | None:
    for _index, line, _newline, _lines in _iter_require_lines(text):
        match = _MODULE_VERSION_RE.match(line)
        if match is not None and _same_module(match.group("module"), package):
            return match.group("version")
    return None


def _rewrite(text: str, package: str, new_version: str) -> tuple[str, str | None]:
    """Rewrite every matching require line; return ``(text, old_version)``."""
    old_version: str | None = None
    lines: list[str] | None = None
    for index, line, newline, all_lines in _iter_require_lines(text):
        lines = all_lines
        match = _MODULE_VERSION_RE.match(line)
        if match is None or not _same_module(match.group("module"), package):
            continue
        old_version = old_version or match.group("version")
        lines[index] = (
            f"{match.group('lead')}{match.group('module')} {new_version}"
            f"{match.group('rest')}{newline}"
        )
    return ("".join(lines) if lines is not None else text), old_version
