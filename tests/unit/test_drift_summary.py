from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.scanners.drift_summary import summarize_dependency_drift
from depfix.scanners.matching import ScanChangeAssessment, ScanMatchStatus
from depfix.scanners.models import DeclaredDependency, RepoScanResult


def _change(new_version: str = "7.20.0") -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="7.19.0",
        new_version=new_version,
        old_api="openai@7.19.0",
        new_api="openai@7.20.0",
        description="drift",
        migration_guide="update",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
        provider_id="openai",
    )


def _result(version: str) -> RepoScanResult:
    return RepoScanResult(
        repo_full_name="acme/app",
        commit_sha="sha",
        dependencies=[DeclaredDependency("openai", "package.json", version, version, "range")],
    )


def test_major_gap_is_report_only_even_when_feed_is_newer() -> None:
    rows = summarize_dependency_drift(
        _result("5.8.2"),
        [ScanChangeAssessment(_change(), ScanMatchStatus.VERSION_NOT_AFFECTED, "major differs")],
    )

    assert rows[0].feed_version == "7.20.0"
    assert rows[0].decision == "major-gap review"
    assert "no automatic edit" in rows[0].risk


def test_actionable_same_major_drift_is_distinguished() -> None:
    rows = summarize_dependency_drift(
        _result("7.19.0"),
        [ScanChangeAssessment(_change(), ScanMatchStatus.ACTIONABLE, "drift")],
    )

    assert rows[0].decision == "actionable"
    assert "eligible for plan" in rows[0].risk


def test_unresolved_catalog_reference_is_not_compared_as_semver() -> None:
    rows = summarize_dependency_drift(
        _result("catalog:"),
        [ScanChangeAssessment(_change(), ScanMatchStatus.NO_CALL_SITES, "none")],
    )

    assert rows[0].decision == "version unresolved"
