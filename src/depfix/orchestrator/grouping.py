"""Group related committable changes into one pull request.

depfix opens one PR per breaking change. On a fleet run that touches many
changes in the same repo, that is exactly the "one PR per dependency" noise
that buries the important updates. This module decides which per-change
results should be *combined* into a single branch and PR, keyed by a group
identity the operator controls.

Grouping is per repository and never crosses one: each repo's PR is its own
review unit. Within a repo, the default grouping key is the provider id, so
"everything about the openai migration" lands in one PR -- but a repo's
.depfix.yml can widen this to "all drift" or narrow it to "one PR each".

Security-advisory changes are never grouped with anything else: a
vulnerability fix must be reviewable and mergeable on its own, on its own
urgency, not held hostage behind an unrelated API migration in the same PR.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from depfix.apply.models import FileEdit
from depfix.core.models import BreakingChange, ChangeKind
from depfix.core.pipeline import FixPipelineResult


def slug(key: str) -> str:
    """Filesystem/branch-safe form of a group key."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-")[:60] or "group"


@dataclass
class ChangeGroup:
    """One PR's worth of changes.

    Every member is guaranteed to touch a disjoint set of files -- see
    ``group_changes``, which splits rather than merges on collision. That
    invariant is what makes ``edits`` safe: each change was fixed in its own
    clone, so two edits to the same path were each computed against the
    pristine original and cannot be combined by taking one and dropping the
    other.
    """

    key: str
    title: str
    members: list[tuple[BreakingChange, FixPipelineResult, list[FileEdit]]] = field(
        default_factory=list
    )

    @property
    def relpaths(self) -> set[str]:
        return {edit.relpath for _c, _r, edits in self.members for edit in edits}

    def collides_with(self, edits: list[FileEdit]) -> bool:
        return bool(self.relpaths & {edit.relpath for edit in edits})

    @property
    def edits(self) -> list[FileEdit]:
        """All committable edits. Safe to concatenate because the grouper
        guarantees no two members touch the same path."""
        return [edit for _c, _r, member_edits in self.members for edit in member_edits]

    @property
    def worst_confidence(self) -> str:
        """A grouped PR is only as trustworthy as its weakest change."""
        from depfix.verify.confidence import ConfidenceTier

        tiers = [result.confidence for _c, result, _e in self.members]
        return min(tiers, key=lambda t: t.rank).value if tiers else ConfidenceTier.NONE.value


def group_key_for(change: BreakingChange, strategy: str, *, ecosystem: str = "") -> str:
    """The grouping key for one change under a strategy.

    Strategies:
      "provider"  -> one PR per provider (the default)
      "ecosystem" -> one PR per package ecosystem
      "all"       -> one PR for the whole repo run
      "none"      -> one PR per change (depfix's original behaviour)
    """
    if change.kind is ChangeKind.SECURITY_ADVISORY:
        return f"security:{change.dedupe_key[:12]}"
    if strategy == "none":
        return f"change:{change.dedupe_key[:12]}"
    if strategy == "all":
        return "all"
    if strategy == "ecosystem":
        return f"ecosystem:{ecosystem}" if ecosystem else f"provider:{change.provider_id}"
    return f"provider:{change.provider_id}"


def group_changes(
    resolved: list[tuple[BreakingChange, FixPipelineResult, list[FileEdit]]],
    *,
    strategy: str,
    ecosystem_for: Callable[[BreakingChange], str] | None = None,
) -> list[ChangeGroup]:
    """Partition committable per-change results into PR-sized groups.

    On a file collision the colliding change is split into its own group
    rather than overwriting the existing member's edit -- two independently
    computed versions of the same file cannot be merged safely here, and
    silently keeping one would ship a PR that looks complete but isn't.
    """
    groups: list[ChangeGroup] = []
    by_key: dict[str, ChangeGroup] = {}

    for change, result, edits in resolved:
        eco = ecosystem_for(change) if ecosystem_for else ""
        key = group_key_for(change, strategy, ecosystem=eco)
        group = by_key.get(key)
        if group is not None and group.collides_with(edits):
            key = f"{key}:split:{change.dedupe_key[:12]}"
            group = by_key.get(key)
        if group is None:
            group = ChangeGroup(key=key, title=_title_for(key, change))
            by_key[key] = group
            groups.append(group)
        group.members.append((change, result, edits))

    return groups


def _title_for(key: str, first_change: BreakingChange) -> str:
    if key.startswith("security:"):
        return f"security: fix {first_change.package}"
    if key == "all":
        return "depfix: grouped dependency updates"
    return f"depfix: updates for {first_change.provider_id}"
