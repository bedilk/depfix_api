"""Value objects for the classify layer."""

from __future__ import annotations

from dataclasses import dataclass, field

from depfix.core.models import BreakingChange


@dataclass
class ClassifyOutcome:
    """Result of classifying one ``ChangeEvent``.

    ``changes`` and ``errors`` are independent: a spec-diff event with zero
    structural deltas above additive severity is a *successful* classification
    of zero changes, not an error.

    ``unclassifiable`` is a third, terminal outcome distinct from both: the
    event carried no signal to classify at all (no ``spec_changes``, empty
    ``body``) rather than a real verdict of "zero changes" or an
    environment/config problem worth retrying. ``classify_pending`` still
    stamps ``classified_at`` for it, same as a real verdict — there is
    nothing a later retry could do differently.
    """

    changes: list[BreakingChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unclassifiable: bool = False
    cost: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors
