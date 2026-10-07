"""Data model for a repo's opt-in ``.depfix.yml`` pipeline config.

A repo is only touched by the fleet orchestrator (Week 6) if it has a
``.depfix.yml`` at its root -- this file is the entire opt-in surface, so
its schema is versioned and validated strictly (see
:mod:`depfix.repoconfig.loader`): a malformed config must fail loudly
rather than silently disabling the repo or silently allowing everything.
"""

from __future__ import annotations

import dataclasses
import re
from functools import lru_cache

SUPPORTED_VERSION = 1


@lru_cache(maxsize=256)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a segment-aware glob into a compiled, anchored regex.

    Unlike :func:`fnmatch.fnmatch`, ``*`` never crosses a ``/`` -- it only
    matches within one path/id segment. ``**`` matches zero or more whole
    segments (``**/foo`` also matches bare ``foo``; ``src/**`` matches
    everything under ``src/``). This matters for both provider ids
    (``openai-*`` should not accidentally match something with a slash in
    it) and file paths (``tests/**`` must not also match ``tests-utils/x``).
    """
    i, n = 0, len(pattern)
    out: list[str] = ["^"]
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                j = i
                while j < n and pattern[j] == "*":
                    j += 1
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    j += 1
                else:
                    out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def glob_match(value: str, pattern: str) -> bool:
    """``True`` if ``value`` matches the segment-aware glob ``pattern``."""
    return _glob_to_regex(pattern).fullmatch(value) is not None


@dataclasses.dataclass(frozen=True)
class ProviderFilter:
    """Which provider ids the pipeline is allowed to act on for this repo.

    ``exclude`` always wins over ``include`` -- a repo owner who excludes a
    provider must be able to trust that no include pattern added later (by
    them or by a shared template) can re-enable it by accident. An empty
    ``include`` means "no restriction" (everything not excluded is
    allowed), not "nothing is allowed".
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def allows(self, provider_id: str) -> bool:
        if any(glob_match(provider_id, pattern) for pattern in self.exclude):
            return False
        if not self.include:
            return True
        return any(glob_match(provider_id, pattern) for pattern in self.include)


@dataclasses.dataclass(frozen=True)
class PathFilter:
    """Which repo-relative file paths the pipeline is allowed to edit.

    Same exclude-wins-over-include semantics as :class:`ProviderFilter`,
    applied to :class:`~depfix.apply.models.FileEdit.relpath` instead of a
    provider id.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def allows(self, relpath: str) -> bool:
        if any(glob_match(relpath, pattern) for pattern in self.exclude):
            return False
        if not self.include:
            return True
        return any(glob_match(relpath, pattern) for pattern in self.include)


@dataclasses.dataclass(frozen=True)
class RepoConfig:
    """Parsed, validated contents of one repo's ``.depfix.yml``."""

    version: int
    enabled: bool = True
    base_branch: str | None = None
    verify: bool = True
    open_pr: bool = True
    draft_pr: bool = False
    max_changes_per_run: int | None = None
    providers: ProviderFilter = dataclasses.field(default_factory=ProviderFilter)
    paths: PathFilter = dataclasses.field(default_factory=PathFilter)
    ignore: tuple[str, ...] = ()
    triage_rules: tuple = ()
    group_prs_by: str = "provider"
    max_repo_mb: int | None = None

    def is_ignored(self, *, provider_id: str, dedupe_key: str) -> bool:
        """Tenant instructions: an unconditional skip list matched against
        either the provider id or the change's
        :attr:`~depfix.core.models.BreakingChange.dedupe_key`. Checked
        first, ahead of any other orchestrator policy -- a repo owner who
        says "don't touch this" must never be overridden by ``--force`` or
        by capability/bookkeeping checks that run after it.
        """
        return any(
            glob_match(provider_id, pattern) or glob_match(dedupe_key, pattern)
            for pattern in self.ignore
        )
