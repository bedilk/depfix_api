from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.scanners.matching import ScanChangeAssessment, ScanMatchStatus
from depfix.scanners.models import DeclaredDependency, RepoScanResult
from depfix.scanners.report import render_scan_report


def test_report_contains_dependency_and_migration_tables() -> None:
    result = RepoScanResult(
        repo_full_name="acme/app",
        commit_sha="abc",
        files_scanned=3,
        dependencies=[DeclaredDependency("openai", "package.json", "5.8.2", "5.8.2", "range")],
    )
    change = BreakingChange(
        package="openai",
        old_version="7.19.0",
        new_version="7.20.0",
        old_api="openai@7.19.0",
        new_api="openai@7.20.0",
        description="drift",
        migration_guide="update",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
        provider_id="openai",
    )

    report = render_scan_report(
        result, [ScanChangeAssessment(change, ScanMatchStatus.VERSION_NOT_AFFECTED, "wrong major")]
    )

    assert "# Depfix scan report" in report
    assert "| `openai` | `5.8.2` | `7.20.0` | major-gap review" in report
    assert "| `openai@7.19.0` | `openai@7.20.0` | version_not_affected" in report
