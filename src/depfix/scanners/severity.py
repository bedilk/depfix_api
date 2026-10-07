"""Predict severity from deterministic call-site evidence."""

from __future__ import annotations

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.scanners.models import CallSite, MatchConfidence
from depfix.severity import EvidenceKind, Severity, cap_severity

_REMOVAL_KINDS = frozenset(
    {
        ChangeKind.METHOD_REMOVED,
        ChangeKind.PARAM_REMOVED,
        ChangeKind.FIELD_REMOVED,
        ChangeKind.ENUM_VALUE_REMOVED,
    }
)


def severity_from_scan(
    change: BreakingChange, call_sites: list[CallSite]
) -> tuple[Severity, EvidenceKind]:
    actionable = [site for site in call_sites if site.is_actionable]
    if not actionable:
        return Severity.NONE, EvidenceKind.NONE
    structural = change.source in {ClassificationSource.SPEC_DIFF, ClassificationSource.REGISTRY}
    high = any(site.confidence is MatchConfidence.HIGH for site in actionable)
    if change.kind is ChangeKind.METHOD_DEPRECATED:
        return (Severity.MEDIUM if high else Severity.LOW), EvidenceKind.STRUCTURAL
    if structural and change.kind in _REMOVAL_KINDS and high:
        return Severity.CRITICAL, EvidenceKind.STRUCTURAL
    if structural and high:
        return Severity.HIGH, EvidenceKind.STRUCTURAL
    if structural:
        return Severity.MEDIUM, EvidenceKind.STRUCTURAL
    proposed = Severity.HIGH if high else Severity.MEDIUM
    return cap_severity(proposed, EvidenceKind.LLM_JUDGEMENT), EvidenceKind.LLM_JUDGEMENT
