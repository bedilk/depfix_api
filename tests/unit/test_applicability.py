from __future__ import annotations

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.scanners.applicability import Applicability, change_applies
from depfix.scanners.models import DeclaredDependency


def _change() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="7.17.0",
        new_version="7.19.0",
        old_api="openai@7.17.0",
        new_api="openai@7.19.0",
        description="d",
        migration_guide="",
        kind=ChangeKind.DEPENDENCY_VERSION_BUMP,
        source=ClassificationSource.REGISTRY,
        provider_id="openai",
    )


def test_major_drift_is_a_human_review_candidate() -> None:
    verdict = change_applies(
        [DeclaredDependency("openai", "package-lock.json", "^4.24.7", "4.24.7", "lockfile")],
        _change(),
    )

    assert verdict.status is Applicability.APPLIES
    assert "human review" in verdict.reason


def test_missing_version_evidence_does_not_hide_call_sites() -> None:
    verdict = change_applies(
        [DeclaredDependency("openai", "package.json", None, None, "manifest")], _change()
    )

    assert verdict.status is Applicability.UNKNOWN
