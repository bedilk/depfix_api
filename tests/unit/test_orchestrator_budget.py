"""Unit tests for :mod:`depfix.orchestrator.budget`."""

from __future__ import annotations

from depfix.orchestrator.budget import RunBudget


def test_unbounded_budget_never_expires() -> None:
    budget = RunBudget(None, None)
    budget.record_cost(1_000_000.0)
    assert budget.expired is False


def test_time_ceiling_expires_immediately_at_zero_seconds() -> None:
    budget = RunBudget(0.0)
    assert budget.expired is True


def test_cost_ceiling_expires_once_recorded_spend_reaches_it() -> None:
    budget = RunBudget(None, max_cost_usd=1.0)
    assert budget.expired is False

    budget.record_cost(0.6)
    assert budget.expired is False
    assert budget.spent_usd == 0.6

    budget.record_cost(0.4)
    assert budget.expired is True
    assert budget.spent_usd == 1.0


def test_either_ceiling_being_crossed_is_enough() -> None:
    """A cheap-but-slow run and a fast-but-expensive run must both trip
    ``expired`` -- it's an OR of the two ceilings, not an AND."""
    slow_but_cheap = RunBudget(0.0, max_cost_usd=100.0)
    assert slow_but_cheap.expired is True

    fast_but_expensive = RunBudget(3600.0, max_cost_usd=1.0)
    fast_but_expensive.record_cost(1.0)
    assert fast_but_expensive.expired is True
