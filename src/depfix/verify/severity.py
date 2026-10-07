"""Confirm severity using verification evidence when it exists."""

from __future__ import annotations

from depfix.core.models import BreakingChange
from depfix.scanners.models import CallSite
from depfix.scanners.severity import severity_from_scan
from depfix.severity import EvidenceKind, Severity
from depfix.verify.verifier import VerificationReport


def severity_from_verification(
    change: BreakingChange, verification: object | None, call_sites: list[CallSite]
) -> tuple[Severity, EvidenceKind]:
    if isinstance(verification, VerificationReport):
        if verification.new_failure_count > 0:
            return Severity.CRITICAL, EvidenceKind.TEST_FAILURE
        if verification.typechecked and verification.new_diagnostics:
            return Severity.CRITICAL, EvidenceKind.TYPE_ERROR
    return severity_from_scan(change, call_sites)
