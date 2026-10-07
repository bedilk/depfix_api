"""Wall-clock + cost budget for one
:meth:`depfix.orchestrator.runner.Orchestrator.run` call.

Cron gives a fleet run a finite window (e.g. an hourly tick) -- without a
budget, one run with a long tail of slow repos could still be verifying
change #40 when the next cron tick fires, doubling up work and advisory-lock
contention (see :mod:`depfix.storage.lock`). A run can just as easily blow
its *cost* budget instead of its time one -- a fleet of large repos each
needing a few LLM calls adds up -- so ``RunBudget`` tracks both ceilings
under one ``expired`` check. ``RunBudget`` is checked *between* units of
work (before starting a new repo or a new change), never mid-flight -- an
in-progress clone/fix/verify/PR is always allowed to finish cleanly rather
than being cut off half-done, and likewise a change already in flight when
the cost ceiling is crossed still gets to record its own actual spend
before the *next* unit of work is refused.
"""

from __future__ import annotations

import time


class RunBudget:
    """Tracks elapsed wall-clock time and cumulative USD spend against
    optional ``max_seconds``/``max_cost_usd`` ceilings.

    Both ``None`` (the default) means unbounded -- :attr:`expired` is
    always ``False``, so callers don't need a separate "is budgeting even
    enabled" branch.
    """

    def __init__(self, max_seconds: float | None, max_cost_usd: float | None = None) -> None:
        self._max_seconds = max_seconds
        self._max_cost_usd = max_cost_usd
        self._start = time.monotonic()
        self._spent_usd = 0.0

    def record_cost(self, cost_usd: float) -> None:
        """Add one change's actual ``FixPipelineResult.total_cost`` to this
        run's running total."""
        self._spent_usd += cost_usd

    @property
    def expired(self) -> bool:
        if self._max_seconds is not None and self.elapsed_seconds >= self._max_seconds:
            return True
        return self._max_cost_usd is not None and self._spent_usd >= self._max_cost_usd

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    @property
    def spent_usd(self) -> float:
        return self._spent_usd
