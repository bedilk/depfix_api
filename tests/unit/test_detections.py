from pathlib import Path

import yaml

from depfix.catalog.detections import (
    build_detections,
    mark_detection_planned,
    record_repository_detections,
)
from depfix.catalog.learned import persist_learned_migrations
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.scanners.matching import ScanChangeAssessment, ScanMatchStatus
from depfix.scanners.models import CallSite, CallSiteKind, MatchConfidence, RepoScanResult
from depfix.sources.semver import drift_risk


def _drift() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.1",
        old_api="openai@3.3.0",
        new_api="openai@4.0.1",
        description="d",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
        provider_id="openai",
    )


def _api() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.1",
        old_api="openai.createChatCompletion",
        new_api="openai.chat.completions.create",
        description="d",
        migration_guide="",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.RELEASE_NOTES,
        provider_id="openai",
    )


def _site() -> CallSite:
    return CallSite(
        filepath="src/chat.js",
        line_number=4,
        column=0,
        line_content="x",
        kind=CallSiteKind.METHOD_CALL,
        confidence=MatchConfidence.HIGH,
        symbol="openai.createChatCompletion",
        provider_id="openai",
    )


def test_drift_risk() -> None:
    assert drift_risk("1.2.0", "1.4.0") == "patch-minor"
    assert drift_risk("3.3.0", "4.0.1") == "major"
    assert drift_risk("3.3.0", "7.0.0") == "multi-major"
    assert drift_risk("4.0.0", "4.0.0") == ""


def test_version_and_api_drift_are_both_listed(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    site = _site()
    result = RepoScanResult("acme/app", "sha1", call_sites=[site])
    entries = build_detections(
        result,
        [
            ScanChangeAssessment(_drift(), ScanMatchStatus.ACTIONABLE, "drift"),
            ScanChangeAssessment(_api(), ScanMatchStatus.ACTIONABLE, "1 site", (site,)),
        ],
        provider_id="openai",
    )

    assert (
        record_repository_detections(
            path, repo_full_name="acme/app", provider_id="openai", detections=entries
        )
        == 2
    )

    doc = yaml.safe_load(path.read_text())
    by_kind = {e["kind"]: e for e in doc["detections"]["acme/app"]}
    assert set(by_kind) == {"version_drift", "api_drift"}
    assert by_kind["version_drift"]["risk"] == "major"
    assert by_kind["version_drift"]["call_site_count"] == 1
    assert doc["migrations"] == {}


def test_version_drift_without_call_sites_is_listed(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    entries = build_detections(
        RepoScanResult("acme/app", "sha1"),
        [ScanChangeAssessment(_drift(), ScanMatchStatus.ACTIONABLE, "drift")],
        provider_id="openai",
    )
    record_repository_detections(
        path, repo_full_name="acme/app", provider_id="openai", detections=entries
    )
    (entry,) = yaml.safe_load(path.read_text())["detections"]["acme/app"]
    assert entry["call_site_count"] == 0


def test_plan_without_scan_creates_entry_and_migrations_survive(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    mark_detection_planned(
        path,
        repo_full_name="acme/app",
        change=_drift(),
        commit_sha="sha1",
        artifact_path="p.json",
        files=["package.json"],
    )
    persist_learned_migrations(path, [_api()])
    doc = yaml.safe_load(path.read_text())
    (entry,) = doc["detections"]["acme/app"]
    assert entry["plan_status"] == "planned"
    assert entry["plan_artifact"] == "p.json"
    assert len(doc["migrations"]) == 1
