"""How much evidence stands behind a fix, and where it came from.

The blunt fact about the fleet is that most repos either have no test suite
or have one that doesn't cover the call site a migration touched. Before
this module existed there were exactly two states -- "tests confirmed it"
and "SUSPECT" -- which meant the overwhelmingly common case (no usable
tests) produced nothing committable, and the operator had no way to say
"open the weakly-evidenced ones as drafts and let me look".

So evidence is now *graded*, and the grade is carried all the way out to
the PR body (see :func:`depfix.gh.pr.build_pr_body`) and the ``fix_run``
row. The ordering is deliberate and is the whole trust model:

``HIGH``
    The repo's own test suite ran, and diffing baseline-vs-after-fix by
    test identity found no new failure. This is the only tier where an
    external oracle we didn't write confirmed the *behavior*.
``MEDIUM``
    No usable test signal, but a type/contract oracle passed -- today
    that means ``tsc --noEmit`` produced no new diagnostics (see
    :mod:`depfix.verify.typecheck`). Strong evidence the migration's
    *shape* is right; silent about behavior.
``LOW``
    Only the syntax/sanity validator ran (see
    :class:`depfix.validators.javascript.FixValidator`). The file parses
    and doesn't obviously contradict the change. That is all.
``NONE``
    Nothing confirmed anything -- a run crashed, output was unparseable,
    or no oracle was available at all.

Deliberately *not* an ordering over "how good the fix is": it is an
ordering over how much we can prove. A perfect fix in a repo with no
tests and no TypeScript is still ``LOW``, and should still be a draft PR.
"""

from __future__ import annotations

from enum import StrEnum


class ConfidenceTier(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_least(self, other: ConfidenceTier) -> bool:
        return self.rank >= other.rank

    @property
    def summary(self) -> str:
        """One line explaining what this tier does and does not prove --
        rendered verbatim into the PR body, because a reviewer deciding
        whether to trust a machine-authored PR needs the caveat, not just
        the label."""
        return _SUMMARY[self]


_RANK: dict[ConfidenceTier, int] = {
    ConfidenceTier.NONE: 0,
    ConfidenceTier.LOW: 1,
    ConfidenceTier.MEDIUM: 2,
    ConfidenceTier.HIGH: 3,
}

_SUMMARY: dict[ConfidenceTier, str] = {
    ConfidenceTier.HIGH: (
        "The repository's own test suite was run before and after this change and "
        "introduced no new failures."
    ),
    ConfidenceTier.MEDIUM: (
        "No usable test signal was available, but a TypeScript typecheck of the "
        "repository introduced no new diagnostics. This confirms the shape of the "
        "migration, not its runtime behaviour."
    ),
    ConfidenceTier.LOW: (
        "Only a syntax/sanity check ran. Neither this repository's tests nor a "
        "typechecker confirmed the change -- please review the diff carefully."
    ),
    ConfidenceTier.NONE: (
        "Nothing confirmed this change: verification could not run or produced no "
        "usable signal. Treat this as a suggestion only."
    ),
}
