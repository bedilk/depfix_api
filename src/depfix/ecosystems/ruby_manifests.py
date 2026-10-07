"""Conservative Gemfile dependency-drift edits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from depfix.codemods.lockfile import dependency_drift_policy
from depfix.core.models import BreakingChange

_SKIP = frozenset({".git", "vendor", "node_modules", ".bundle"})
_GEM_LINE_RE = re.compile(
    r"""^(?P<indent>\s*)gem\s+(?P<q>['"])(?P<name>[\w.-]+)(?P=q)"""
    r"""(?P<constraints>(?:\s*,\s*['"][^'"]*['"])*)"""
    r"""(?P<rest>.*)$""",
    re.MULTILINE,
)
_CONSTRAINT_RE = re.compile(r"""['"](?P<spec>[^'"]*)['"]""")
_VERSION_IN_SPEC_RE = re.compile(r"(\d+)(?:\.\d+)*")


@dataclass(frozen=True)
class GemfileBump:
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
            f"{self.new_spec!r}; run `bundle lock` before merging"
        )


def _spec_major(spec: str) -> int | None:
    match = _VERSION_IN_SPEC_RE.search(spec)
    return int(match.group(1)) if match else None


def build_ruby_dependency_drift_bumps(root: Path, change: BreakingChange) -> list[GemfileBump]:
    if dependency_drift_policy(change) != "automatic":
        return []
    new_version = change.new_version.strip().lstrip("v")
    if not new_version:
        return []

    bumps: list[GemfileBump] = []
    for path in _iter_gemfiles(root):
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fixed, old_spec = _rewrite_gemfile(original, change.package, new_version)
        if old_spec is None or fixed == original:
            continue
        if (
            _spec_major(old_spec) is not None
            and _spec_major(new_version) is not None
            and _spec_major(old_spec) != _spec_major(new_version)
        ):
            continue
        bumps.append(
            GemfileBump(
                path.relative_to(root).as_posix(),
                original,
                fixed,
                change.package,
                old_spec,
                f"~> {new_version}",
            )
        )
    return bumps


def _iter_gemfiles(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in ("Gemfile", "*.gemspec"):
        for path in sorted(root.rglob(name)):
            if any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
                continue
            found.append(path)
    return found


def _rewrite_gemfile(text: str, package: str, new_version: str) -> tuple[str, str | None]:
    old_spec: str | None = None

    def _replace(match: re.Match[str]) -> str:
        nonlocal old_spec
        if match.group("name") != package:
            return match.group(0)
        constraints = _CONSTRAINT_RE.findall(match.group("constraints"))
        if not constraints:
            return match.group(0)
        if old_spec is None:
            old_spec = ", ".join(constraints)
        q = match.group("q")
        return (
            f"{match.group('indent')}gem {q}{package}{q}, "
            f"{q}~> {new_version}{q}{match.group('rest')}"
        )

    return _GEM_LINE_RE.sub(_replace, text), old_spec
