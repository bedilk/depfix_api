"""The codemod contract.

Three outcomes, and the distinction between them is load-bearing:

``rewrote``
    The rule matched, rewrote every occurrence, and verified its own
    completeness. The result may be committed without an LLM call.
``unchanged``
    The rule applies to this change but found nothing to do in this file --
    normal, and not a failure.
``decline``
    The rule matched something it cannot finish safely. The reason is
    surfaced to the operator and the file falls through to the language
    model. Declining is a success for this layer, not an error: a partial
    migration compiles, passes review, and breaks at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from depfix.core.models import BreakingChange

__all__ = ["Codemod", "CodemodResult"]


@dataclass(frozen=True, slots=True)
class CodemodResult:
    """What a codemod did to one file."""

    codemod_id: str
    #: Rewritten source, or ``None`` when nothing was written.
    source: str | None = None
    changed: bool = False
    #: Why the rule refused. Non-empty only for declines.
    declined_reason: str = ""
    #: Short operator-facing summary, shown for every outcome.
    note: str = ""

    @property
    def declined(self) -> bool:
        return bool(self.declined_reason)

    @classmethod
    def rewrote(cls, codemod_id: str, source: str, note: str = "") -> CodemodResult:
        return cls(codemod_id=codemod_id, source=source, changed=True, note=note)

    @classmethod
    def unchanged(cls, codemod_id: str, note: str = "") -> CodemodResult:
        return cls(codemod_id=codemod_id, source=None, changed=False, note=note)

    @classmethod
    def decline(cls, codemod_id: str, reason: str) -> CodemodResult:
        return cls(
            codemod_id=codemod_id, source=None, changed=False, declined_reason=reason, note=reason
        )


@runtime_checkable
class Codemod(Protocol):
    """A reviewed deterministic rewrite for a class of breaking change."""

    #: Stable, namespaced identifier. Persisted per edit and printed in PR
    #: bodies, so it must never change silently -- it is how a bad rewrite
    #: found in production is traced back to the rule that produced it.
    id: str

    #: Higher wins when several rules claim the same change. A rule that
    #: knows one library's migration end-to-end should beat a generic
    #: pattern, because it can also handle the library's response-shape
    #: changes rather than just the rename.
    specificity: int

    def claims(self, change: BreakingChange) -> bool:
        """Whether this rule believes it understands ``change``."""
        ...

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        """Rewrite ``source``, or decline with a reason."""
        ...
