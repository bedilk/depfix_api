from depfix.classify.severity import EvidenceKind, Severity, cap_severity


def test_critical_llm_judgement_is_capped_at_high() -> None:
    assert cap_severity(Severity.CRITICAL, EvidenceKind.LLM_JUDGEMENT) is Severity.HIGH


def test_critical_structural_evidence_remains_critical() -> None:
    assert cap_severity(Severity.CRITICAL, EvidenceKind.STRUCTURAL) is Severity.CRITICAL
