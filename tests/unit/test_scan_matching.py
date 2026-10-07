"""Tests for the commit-specific scan-to-change matching seam."""

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.scanners.matching import ScanMatchStatus, assess_scan_change
from depfix.scanners.models import (
    CallSite,
    CallSiteKind,
    DeclaredDependency,
    MatchConfidence,
    RepoScanResult,
)


def _change(old_api: str = "openai.createChatCompletion") -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.0",
        old_api=old_api,
        new_api="openai.chat.completions.create",
        description="migration",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
    )


def _result(*, symbol: str = "openai.createChatCompletion") -> RepoScanResult:
    return RepoScanResult(
        repo_full_name="acme/widgets",
        commit_sha="abc123",
        dependencies=[
            DeclaredDependency(
                package="openai",
                manifest_path="package.json",
                declared_range="^3.3.0",
                resolved_version="3.3.0",
                source="package.json",
            )
        ],
        call_sites=[
            CallSite(
                filepath="src/openai.js",
                line_number=4,
                column=0,
                line_content="openai.createChatCompletion({})",
                kind=CallSiteKind.METHOD_CALL,
                confidence=MatchConfidence.HIGH,
                symbol=symbol,
                provider_id="openai",
            )
        ],
    )


def test_assessment_is_actionable_for_an_affected_exact_call_site() -> None:
    assessment = assess_scan_change(_result(), _change())

    assert assessment.status is ScanMatchStatus.ACTIONABLE
    assert len(assessment.matched_sites) == 1


def test_assessment_records_no_call_sites_for_an_affected_but_unmatched_change() -> None:
    assessment = assess_scan_change(_result(), _change("openai.createEmbedding"))

    assert assessment.status is ScanMatchStatus.NO_CALL_SITES
    assert assessment.matched_sites == ()


def test_assessment_marks_a_repo_current_when_it_uses_the_feed_replacement() -> None:
    assessment = assess_scan_change(_result(symbol="openai.chat.completions.create"), _change())

    assert assessment.status is ScanMatchStatus.CURRENT


def test_assessment_rejects_version_not_affected_before_call_site_matching() -> None:
    result = _result()
    result.dependencies[0] = DeclaredDependency(
        package="openai",
        manifest_path="package.json",
        declared_range="^4.0.0",
        resolved_version="4.1.0",
        source="package.json",
    )

    assessment = assess_scan_change(result, _change())

    assert assessment.status is ScanMatchStatus.VERSION_NOT_AFFECTED
