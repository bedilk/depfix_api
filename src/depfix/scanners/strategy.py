"""One authoritative resolver for which scan strategy runs.

Before this module, the string ``"agent" if coordinator is not None else
"deterministic"`` was duplicated at two call sites (the CLI _cmd_scan and
the fleet orchestrator _process_change_body), and both were wrong: with the
default ``SCAN_STRATEGY=hybrid`` plus any LLM configured, they forced
*pure-agent* scanning -- the exact thing ``docs/decisions.md`` records as
rejected ("0 call sites for $3.16, worse than deterministic on both measures").
The pure agent never walks the filesystem, so ``files_scanned`` came from
the model's JSON and every scan reported 0.

This module is the single source of truth. Call sites pass what the user
asked for (``settings.scan_strategy``) and whether an LLM is available;
they never assemble the decision from a coordinator being non-None.
"""

from __future__ import annotations

from enum import StrEnum


class ScanStrategy(StrEnum):
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    AGENT = "agent"


def resolve_scan_strategy(requested: str, *, has_llm: bool) -> ScanStrategy:
    """Resolve the effective strategy from the requested one and LLM availability.

    - ``agent`` requires an LLM and never silently downgrades (refusing is
      the documented contract in ``scan_checkout``).
    - ``hybrid`` falls back to ``deterministic`` when no LLM is available,
      so the deterministic pass still runs free.
    - ``deterministic`` is always itself.
    """
    strategy = ScanStrategy(requested)
    if strategy is ScanStrategy.AGENT and not has_llm:
        raise ValueError(
            "SCAN_STRATEGY=agent requires an LLM (set GOOGLE_API_KEY, "
            "LLM_PROVIDER=ollama, or configure Bedrock). Set "
            "SCAN_STRATEGY=deterministic explicitly if that is what you want."
        )
    if strategy is ScanStrategy.HYBRID and not has_llm:
        return ScanStrategy.DETERMINISTIC
    return strategy
