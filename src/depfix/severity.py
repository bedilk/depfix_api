"""Dependency-neutral severity labels and evidence provenance."""

from __future__ import annotations

import enum


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_least(self, other: Severity) -> bool:
        return self.rank >= other.rank


class EvidenceKind(enum.StrEnum):
    TEST_FAILURE = "test_failure"
    TYPE_ERROR = "type_error"
    STRUCTURAL = "structural"
    LLM_JUDGEMENT = "llm_judgement"
    NONE = "none"


_RANK = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}
CRITICAL_EVIDENCE = frozenset(
    {EvidenceKind.TEST_FAILURE, EvidenceKind.TYPE_ERROR, EvidenceKind.STRUCTURAL}
)


def cap_severity(severity: Severity, evidence: EvidenceKind) -> Severity:
    return (
        Severity.HIGH
        if severity is Severity.CRITICAL and evidence not in CRITICAL_EVIDENCE
        else severity
    )
