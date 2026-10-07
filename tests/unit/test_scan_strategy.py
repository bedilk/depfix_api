"""The scan strategy must come from Settings, never from coordinator-is-not-None."""

from __future__ import annotations

import pytest

from depfix.scanners.strategy import ScanStrategy, resolve_scan_strategy


def test_hybrid_with_llm_stays_hybrid_not_agent() -> None:
    # The exact bug: hybrid + LLM must NOT resolve to pure-agent.
    assert resolve_scan_strategy("hybrid", has_llm=True) is ScanStrategy.HYBRID


def test_hybrid_without_llm_falls_back_to_deterministic() -> None:
    assert resolve_scan_strategy("hybrid", has_llm=False) is ScanStrategy.DETERMINISTIC


def test_agent_without_llm_refuses_rather_than_downgrading() -> None:
    with pytest.raises(ValueError, match="requires an LLM"):
        resolve_scan_strategy("agent", has_llm=False)


def test_agent_with_llm_stays_agent() -> None:
    assert resolve_scan_strategy("agent", has_llm=True) is ScanStrategy.AGENT


def test_deterministic_ignores_llm_availability() -> None:
    assert resolve_scan_strategy("deterministic", has_llm=True) is ScanStrategy.DETERMINISTIC
    assert resolve_scan_strategy("deterministic", has_llm=False) is ScanStrategy.DETERMINISTIC
