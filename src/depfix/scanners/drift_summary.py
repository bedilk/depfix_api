"""Explain repository SDK versions against the versions observed in feeds.

This module is deliberately report-only.  A version gap is useful operator
information, but it is not permission to change code: plan/apply still use
``ScanChangeAssessment`` and require a feed-proven migration or a safe
same-major dependency bump.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from depfix.scanners.matching import ScanChangeAssessment, ScanMatchStatus
from depfix.scanners.models import RepoScanResult
from depfix.sources.semver import parse_semver


@dataclass(frozen=True)
class DependencyDriftRow:
    """One dependency's repository-versus-feed comparison for scan output."""

    package: str
    current_version: str
    feed_version: str
    decision: str
    risk: str


def summarize_dependency_drift(
    result: RepoScanResult,
    assessments: Iterable[ScanChangeAssessment],
) -> list[DependencyDriftRow]:
    """Return one concise, conservative version/risk row per SDK dependency.

    ``feed_version`` is the highest version in the classified feed records
    available to this scan.  It is an observed comparison point, not a claim
    that an arbitrary major upgrade is automatically safe.
    """
    assessment_list = list(assessments)
    rows: list[DependencyDriftRow] = []
    seen: set[tuple[str, str]] = set()
    for dependency in result.dependencies:
        current = dependency.resolved_version or dependency.declared_range or "unknown"
        key = (dependency.package, current)
        if key in seen:
            continue
        seen.add(key)

        relevant = [
            assessment
            for assessment in assessment_list
            if assessment.change.package == dependency.package
        ]
        feed_version = _highest_feed_version(relevant)
        rows.append(
            DependencyDriftRow(
                package=dependency.package,
                current_version=current,
                feed_version=feed_version or "not observed",
                decision=_decision(current, feed_version, relevant),
                risk=_risk(current, feed_version, relevant),
            )
        )
    return rows


def _highest_feed_version(assessments: list[ScanChangeAssessment]) -> str:
    versions = [assessment.change.new_version for assessment in assessments]
    parsed = [(parse_semver(version), version) for version in versions if parse_semver(version)]
    if not parsed:
        return ""
    return max(parsed, key=lambda item: item[0])[1]  # type: ignore[arg-type,return-value]


def _decision(
    current: str,
    feed: str,
    assessments: list[ScanChangeAssessment],
) -> str:
    current_semver, feed_semver = parse_semver(current), parse_semver(feed)
    if current_semver is None or feed_semver is None:
        return "version unresolved"
    if current_semver[0] != feed_semver[0]:
        return "major-gap review"
    if current_semver < feed_semver:
        if any(a.status is ScanMatchStatus.ACTIONABLE for a in assessments):
            return "actionable"
        return "same-major drift"
    return "current or newer"


def _risk(
    current: str,
    feed: str,
    assessments: list[ScanChangeAssessment],
) -> str:
    current_semver, feed_semver = parse_semver(current), parse_semver(feed)
    if current_semver is None or feed_semver is None:
        return "No resolved semver comparison; inspect the manifest or lockfile."
    if current_semver[0] != feed_semver[0]:
        return (
            "Major-version gap: review migrations before upgrading; no automatic edit is planned."
        )
    if any(a.status is ScanMatchStatus.ACTIONABLE for a in assessments):
        return "Feed-proven migration matches repository usage; eligible for plan."
    if current_semver < feed_semver:
        return "Same-major version drift: review the feed change before updating."
    return "No version risk detected from the feed records used by this scan."
