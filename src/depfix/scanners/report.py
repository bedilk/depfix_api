"""Markdown scan reports that can be stored beside the scanned repository."""

from __future__ import annotations

from depfix.scanners.drift_summary import summarize_dependency_drift
from depfix.scanners.matching import ScanChangeAssessment
from depfix.scanners.models import RepoScanResult


def render_scan_report(result: RepoScanResult, assessments: list[ScanChangeAssessment]) -> str:
    """Render a compact, self-contained report without exposing source snippets."""
    lines = [
        "# Depfix scan report",
        "",
        "| Repository | Commit | Files scanned | Call sites | Actionable sites |",
        "| --- | --- | ---: | ---: | ---: |",
        (
            f"| `{result.repo_full_name}` | `{result.commit_sha}` | {result.files_scanned} | "
            f"{len(result.call_sites)} | {len(result.actionable_sites)} |"
        ),
        "",
        "## Dependency drift",
        "",
        "| Package | Repository version | Feed version | Decision | Risk if ignored |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows = summarize_dependency_drift(result, assessments)
    if rows:
        lines.extend(
            f"| `{row.package}` | `{row.current_version}` | `{row.feed_version}` | "
            f"{row.decision} | {row.risk} |"
            for row in rows
        )
    else:
        lines.append(
            "| — | — | — | no provider SDK manifest found | No version comparison available. |"
        )

    lines.extend(
        [
            "",
            "## Feed-proven migration decisions",
            "",
            "| Old API | Replacement | Decision | Evidence |",
            "| --- | --- | --- | --- |",
        ]
    )
    if assessments:
        lines.extend(
            f"| `{assessment.change.old_api or 'version drift'}` | "
            f"`{assessment.change.new_api or '—'}` | {assessment.status.value} | "
            f"{_escape(assessment.reason)} |"
            for assessment in assessments
        )
    else:
        lines.append("| — | — | no classified feed changes | Scan recorded usage only. |")

    lines.extend(
        [
            "",
            "A feed version is comparison evidence, not automatic permission to perform a major upgrade.",
            "",
        ]
    )
    return "\n".join(lines)


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
