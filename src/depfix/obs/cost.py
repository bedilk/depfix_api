"""Process-wide LLM cost accounting.

Most of depfix costs nothing, and that is a design property, not a missing
feature. Scanning is AST/regex, feed polling is HTTP, deterministic
classification and codemods and manifest bumps never call a model.

LLM spend enters at exactly five seams, and each already computes its own
per-call cost from a provider pricing table:

* release-notes / synthesis / usage-candidate classification
* fix generation (+ agent tool loop + retry loop)
* characterization-fixture generation
* the optional LLM call-site judge

Before this, only the fix-generation total survived to the outcome; the
classify-time and characterization costs were computed and discarded.  This
ledger is the one place all five seams add into, so the number a run
reports is the number it actually spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CostStage(StrEnum):
    CLASSIFY = "classify"
    FIX_GENERATION = "fix_generation"
    RETRY = "retry"
    CHARACTERIZATION = "characterization"
    CALL_SITE_JUDGE = "call_site_judge"
    SCAN_AGENT = "scan_agent"
    SYMBOL_ALIGNMENT = "symbol_alignment"


@dataclass
class CostLedger:
    """Accumulates LLM spend across one logical run."""

    by_stage: dict[str, float] = field(default_factory=dict)
    call_count: dict[str, int] = field(default_factory=dict)
    tokens_by_stage: dict[str, int] = field(default_factory=dict)

    def record(
        self,
        stage: CostStage,
        usd: float,
        *,
        calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.by_stage[stage.value] = self.by_stage.get(stage.value, 0.0) + max(0.0, usd)
        self.call_count[stage.value] = self.call_count.get(stage.value, 0) + calls
        self.tokens_by_stage[stage.value] = (
            self.tokens_by_stage.get(stage.value, 0) + input_tokens + output_tokens
        )

    @property
    def total(self) -> float:
        return sum(self.by_stage.values())

    @property
    def total_calls(self) -> int:
        return sum(self.call_count.values())

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens_by_stage.values())

    def merge(self, other: CostLedger) -> None:
        for stage, usd in other.by_stage.items():
            self.by_stage[stage] = self.by_stage.get(stage, 0.0) + usd
        for stage, calls in other.call_count.items():
            self.call_count[stage] = self.call_count.get(stage, 0) + calls
        for stage, tokens in other.tokens_by_stage.items():
            self.tokens_by_stage[stage] = self.tokens_by_stage.get(stage, 0) + tokens

    def snapshot(self) -> CostLedger:
        return CostLedger(
            by_stage=dict(self.by_stage),
            call_count=dict(self.call_count),
            tokens_by_stage=dict(self.tokens_by_stage),
        )

    def delta_since(self, snapshot: CostLedger) -> CostLedger:
        delta = CostLedger()
        for stage, usd in self.by_stage.items():
            diff = usd - snapshot.by_stage.get(stage, 0.0)
            if diff > 0:
                delta.by_stage[stage] = diff
                delta.call_count[stage] = self.call_count.get(stage, 0) - snapshot.call_count.get(
                    stage, 0
                )
                delta.tokens_by_stage[stage] = self.tokens_by_stage.get(
                    stage, 0
                ) - snapshot.tokens_by_stage.get(stage, 0)
        return delta

    def summary(self) -> str:
        if not self.by_stage:
            return "no LLM calls"
        parts = ", ".join(
            f"{stage}: ${usd:.4f} ({self.call_count.get(stage, 0)})"
            for stage, usd in sorted(self.by_stage.items())
        )
        tts = f"  TTS: {self.total_tokens:,} tokens" if self.total_tokens else ""
        return f"${self.total:.4f} across {self.total_calls} call(s) — {parts}{tts}"
