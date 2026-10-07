"""Per-repository drift detections, listed in ``.depfix/learned_migrations.yaml``.

A detection is anything scan/plan found that would change this repository:

* ``version_drift``      -- the installed SDK is behind the feed. Listed whether
  or not any call site uses the SDK; the call sites found are attached.
* ``api_drift``          -- a feed-documented API change with matching call sites.
* ``security_advisory``  -- an advisory that applies to the installed version.

Detections are *not* migrations: they are repository- and commit-specific, so
they live in their own section and are never re-imported into the database.
Each one carries ``plan_status`` (detected -> planned | report_only -> pr_opened)
and a pointer to the plan artifact ``depfix apply`` consumes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from depfix.catalog.learned import _read_document, _write_document
from depfix.core.models import BreakingChange, ChangeKind
from depfix.sources.semver import drift_risk

if TYPE_CHECKING:
    from depfix.scanners.matching import ScanChangeAssessment
    from depfix.scanners.models import CallSite, RepoScanResult

VERSION_DRIFT = "version_drift"
API_DRIFT = "api_drift"
SECURITY_ADVISORY = "security_advisory"

STATUS_DETECTED = "detected"
STATUS_PLANNED = "planned"
STATUS_REPORT_ONLY = "report_only"
STATUS_PR_OPENED = "pr_opened"

_MAX_SITES = 25
_CARRIED_FIELDS = ("plan_status", "plan_artifact", "planned_files", "planned_at", "pr_url")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def detection_kind(change: BreakingChange) -> str:
    if change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP:
        return VERSION_DRIFT
    if change.kind is ChangeKind.SECURITY_ADVISORY:
        return SECURITY_ADVISORY
    return API_DRIFT


def _site(site: CallSite) -> dict:
    return {
        "file": site.filepath,
        "line": site.line_number,
        "symbol": site.symbol,
        "confidence": site.confidence.value,
    }


def _base_entry(change: BreakingChange, *, commit_sha: str) -> dict:
    kind = detection_kind(change)
    entry: dict = {
        "detection_key": change.dedupe_key,
        "kind": kind,
        "provider_id": change.provider_id,
        "package": change.package,
        "change_kind": change.kind.value,
        "old_version": change.old_version,
        "new_version": change.new_version,
        "old_api": change.old_api,
        "new_api": change.new_api,
        "source": change.source.value,
        "source_url": change.source_url or "",
        "evidence": change.evidence or "",
        "commit_sha": commit_sha,
        "detected_at": _now(),
        "plan_status": STATUS_DETECTED,
        "plan_artifact": "",
        "call_site_count": 0,
        "call_sites": [],
    }
    if kind == VERSION_DRIFT:
        entry["risk"] = drift_risk(change.old_version, change.new_version) or "unknown"
    return entry


def build_detections(
    result: RepoScanResult,
    assessments: Iterable[ScanChangeAssessment],
    *,
    provider_id: str,
) -> list[dict]:
    """One entry per actionable assessment.

    A version drift lists every actionable provider call site (the bump may
    affect any of them, and zero is a valid answer). An API drift lists only
    the call sites that matched its old API.

    When the agent scanner ran, ``result.discovered_anchors`` carries verified
    old-API call sites and ``result.api_mapping`` carries old→new pairs; both
    are attached to every detection so the fix pipeline can generate targeted
    code changes without re-scanning.
    """
    from depfix.scanners.matching import ScanMatchStatus

    provider_sites = [
        s for s in result.actionable_sites if not s.provider_id or s.provider_id == provider_id
    ]
    # Agent-discovered anchors and mapping (present only on agent-path scans).
    anchors = getattr(result, "discovered_anchors", []) or []
    api_mapping = getattr(result, "api_mapping", []) or []

    out: list[dict] = []
    for assessment in assessments:
        if assessment.status is not ScanMatchStatus.ACTIONABLE:
            continue
        entry = _base_entry(assessment.change, commit_sha=result.commit_sha)
        sites = provider_sites if entry["kind"] == VERSION_DRIFT else list(assessment.matched_sites)
        entry["call_site_count"] = len(sites)
        entry["call_sites"] = [_site(s) for s in sites[:_MAX_SITES]]
        entry["reason"] = assessment.reason
        if anchors:
            entry["anchors"] = anchors[:_MAX_SITES]
        if api_mapping:
            entry["api_mapping"] = api_mapping
        out.append(entry)
    return out


def build_anchor_detection(
    result: RepoScanResult,
    *,
    provider_id: str,
) -> dict | None:
    """Build a bare detection record for agent-discovered anchors when no
    classified breaking changes matched (e.g. feeds don't have v3→v4 records).

    This keeps the anchors visible in learned_migrations.yaml even when the
    standard assessment pipeline found nothing actionable.
    """
    anchors = getattr(result, "discovered_anchors", []) or []
    api_mapping = getattr(result, "api_mapping", []) or []
    if not anchors and not api_mapping:
        return None
    from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource

    synthetic = BreakingChange(
        provider_id=provider_id,
        package=f"{provider_id}:agent-anchors",
        old_version="",
        new_version="",
        old_api="(agent-discovered)",
        new_api="(see api_mapping)",
        description=f"Agent-discovered old-API call sites for {provider_id}",
        migration_guide="",
        kind=ChangeKind.API_RENAME,  # type: ignore[attr-defined]
        source=ClassificationSource.AGENT,
    )
    entry = _base_entry(synthetic, commit_sha=result.commit_sha)
    entry["kind"] = "agent_anchors"
    entry["call_site_count"] = len(anchors)
    entry["call_sites"] = anchors[:_MAX_SITES]
    entry["anchors"] = anchors[:_MAX_SITES]
    entry["api_mapping"] = api_mapping
    entry["reason"] = (
        "agent-discovered old-API call sites pending feed-classified migration records"
    )
    return entry


def record_repository_detections(
    path: str | Path, *, repo_full_name: str, provider_id: str, detections: list[dict]
) -> int:
    """Replace one (repo, provider) snapshot. Plan/PR status survives a rescan
    of the same commit, because a plan artifact is tied to its base sha."""
    path = Path(path)
    document = _read_document(path)
    repos = document["detections"]
    existing = repos.get(repo_full_name, [])
    if not detections and not any(e.get("provider_id") == provider_id for e in existing):
        return 0
    previous = {e.get("detection_key"): e for e in existing}
    kept = [e for e in existing if e.get("provider_id") != provider_id]
    for entry in detections:
        prior = previous.get(entry["detection_key"])
        if prior and prior.get("commit_sha") == entry["commit_sha"]:
            for field in _CARRIED_FIELDS:
                if prior.get(field):
                    entry[field] = prior[field]
        kept.append(entry)
    if kept:
        repos[repo_full_name] = kept
    else:
        repos.pop(repo_full_name, None)
    return len(detections) if _write_document(path, document) else 0


def mark_detection_planned(
    path: str | Path,
    *,
    repo_full_name: str,
    change: BreakingChange,
    commit_sha: str,
    artifact_path: str,
    files: list[str],
) -> bool:
    """Record that ``depfix plan`` wrote an artifact. Creates the entry when
    plan ran without a prior scan."""
    path = Path(path)
    document = _read_document(path)
    entries = document["detections"].setdefault(repo_full_name, [])
    entry = next((e for e in entries if e.get("detection_key") == change.dedupe_key), None)
    if entry is None:
        entry = _base_entry(change, commit_sha=commit_sha)
        entries.append(entry)
    entry.update(
        commit_sha=commit_sha,
        plan_status=STATUS_PLANNED if files else STATUS_REPORT_ONLY,
        plan_artifact=artifact_path,
        planned_files=list(files),
        planned_at=_now(),
    )
    return _write_document(path, document)


def mark_detection_status(
    path: str | Path,
    *,
    repo_full_name: str,
    dedupe_keys: Iterable[str],
    status: str,
    pr_url: str = "",
    plan_artifact: str = "",
) -> int:
    """Update existing entries only (e.g. upgrade members, or ``pr_opened``)."""
    path = Path(path)
    document = _read_document(path)
    wanted = set(dedupe_keys)
    touched = 0
    for entry in document["detections"].get(repo_full_name, []):
        if entry.get("detection_key") not in wanted:
            continue
        entry["plan_status"] = status
        if pr_url:
            entry["pr_url"] = pr_url
        if plan_artifact:
            entry["plan_artifact"] = plan_artifact
        touched += 1
    if touched and not _write_document(path, document):
        return 0
    return touched
