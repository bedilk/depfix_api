"""Determine whether a classified change can affect a scanned repository."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from depfix.core.models import BreakingChange, ChangeKind
from depfix.scanners.manifest import (
    _parse_semver,
    affects_version,
    declared_major,
    range_allows_major,
)
from depfix.scanners.models import DeclaredDependency


class Applicability(StrEnum):
    APPLIES = "applies"
    NOT_AFFECTED = "not_affected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ApplicabilityVerdict:
    status: Applicability
    reason: str

    @property
    def blocks(self) -> bool:
        return self.status is Applicability.NOT_AFFECTED


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _same_package(declared: str, wanted: str) -> bool:
    return declared == wanted or _normalize(declared) == _normalize(wanted)


def _decide_one(dep: DeclaredDependency, change: BreakingChange) -> ApplicabilityVerdict:
    new = _parse_semver(change.new_version)
    if new is None:
        return ApplicabilityVerdict(Applicability.UNKNOWN, "change version is not plain semver")
    is_drift = change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP
    if dep.resolved_version:
        resolved = _parse_semver(dep.resolved_version)
        if resolved is not None:
            if is_drift and resolved[0] != new[0]:
                return ApplicabilityVerdict(
                    Applicability.APPLIES,
                    f"{dep.manifest_path} resolves {dep.package} to {dep.resolved_version}; "
                    f"major drift to {change.new_version} requires human review",
                )
            affected = affects_version(dep.resolved_version, change.old_version, change.new_version)
            if affected is True:
                return ApplicabilityVerdict(
                    Applicability.APPLIES,
                    f"{dep.manifest_path} resolves {dep.package} to {dep.resolved_version}, "
                    f"before {change.new_version}",
                )
            if affected is False:
                return ApplicabilityVerdict(
                    Applicability.NOT_AFFECTED,
                    f"{dep.manifest_path} already resolves {dep.package} to {dep.resolved_version}",
                )
    if dep.declared_range:
        old = _parse_semver(change.old_version)
        current_major = declared_major(dep.declared_range)
        if is_drift and current_major is not None and current_major != new[0]:
            return ApplicabilityVerdict(
                Applicability.APPLIES,
                f"{dep.manifest_path} pins {dep.package} to {dep.declared_range}; "
                f"major drift to {change.new_version} requires human review",
            )
        target_major = new[0] if is_drift else (old[0] if old is not None else new[0])
        if not is_drift and range_allows_major(dep.declared_range, target_major) is False:
            # For an API migration (not a drift), a range that can't reach the
            # affected major genuinely means the call sites don't apply.
            if current_major is not None and current_major != target_major:
                reason = (
                    f"{dep.manifest_path} pins {dep.package} to {dep.declared_range!r} (major {current_major}), "
                    f"which cannot resolve to major {target_major}. This crosses a major version boundary and "
                    "requires manual review before upgrading."
                )
            else:
                reason = (
                    f"{dep.manifest_path} pins {dep.package} {dep.declared_range!r}, "
                    f"which cannot resolve to major {target_major}"
                )
            return ApplicabilityVerdict(Applicability.NOT_AFFECTED, reason)
        return ApplicabilityVerdict(
            Applicability.UNKNOWN,
            f"{dep.manifest_path} has no resolved lockfile version for {dep.package}",
        )
    return ApplicabilityVerdict(
        Applicability.UNKNOWN, f"no usable version evidence for {dep.package}"
    )


def change_applies(
    dependencies: list[DeclaredDependency], change: BreakingChange
) -> ApplicabilityVerdict:
    """Return a conservative applicability verdict for a repository.

    One applicable workspace is sufficient in a monorepo. Missing or
    unparseable data remains unknown, so it never hides visible call sites.
    """
    relevant = [
        dependency
        for dependency in dependencies
        if _same_package(dependency.package, change.package)
    ]
    if not relevant:
        return ApplicabilityVerdict(
            Applicability.UNKNOWN,
            f"{change.package} is not declared in any manifest depfix scanned",
        )
    verdicts = [_decide_one(dependency, change) for dependency in relevant]
    applies = next(
        (verdict for verdict in verdicts if verdict.status is Applicability.APPLIES), None
    )
    if applies is not None:
        return applies
    if all(verdict.status is Applicability.NOT_AFFECTED for verdict in verdicts):
        return ApplicabilityVerdict(
            Applicability.NOT_AFFECTED, "; ".join(v.reason for v in verdicts)
        )
    return next(verdict for verdict in verdicts if verdict.status is Applicability.UNKNOWN)
