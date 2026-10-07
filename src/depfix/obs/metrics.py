"""Lightweight metrics for depfix verification stages.

Tracks per-(stage, status, skip_reason) counts so operators can distinguish
genuinely un-testable repos (smoke:skipped:no_entrypoint) from config gaps
(characterization:skipped:no_llm_available) from real regressions caught
(characterization:failed, smoke:failed).

Usage:
    from depfix.obs.metrics import verification_stage_outcomes
    verification_stage_outcomes.record(stage="smoke", status="skipped",
                                       skip_reason="no_entrypoint")

In-process only — no external dependency required. Prometheus exporters
can import verification_stage_outcomes and read .counts().
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class _Key:
    stage: str
    status: str
    skip_reason: str


class StageOutcomeCounter:
    """Simple counter: (stage, status, skip_reason) → int."""

    def __init__(self) -> None:
        self._counts: dict[_Key, int] = defaultdict(int)

    def record(
        self,
        *,
        stage: str,
        status: str,
        skip_reason: str = "",
    ) -> None:
        self._counts[_Key(stage=stage, status=status, skip_reason=skip_reason)] += 1

    def record_stage_result(self, result: object) -> None:
        """Accept a StageResult directly."""
        self.record(
            stage=getattr(result, "stage", "unknown"),
            status=str(getattr(result, "status", "unknown")),
            skip_reason=getattr(result, "skip_reason", None) or "",
        )

    def counts(self) -> dict[tuple[str, str, str], int]:
        return {(k.stage, k.status, k.skip_reason): v for k, v in self._counts.items()}

    def summary(self) -> str:
        lines = []
        for (stage, status, reason), count in sorted(self.counts().items()):
            label = f"{stage}:{status}" + (f":{reason}" if reason else "")
            lines.append(f"{label:<50} {count}")
        return "\n".join(lines) if lines else "(no observations)"


# Module-level singleton — import and record from anywhere.
verification_stage_outcomes = StageOutcomeCounter()
