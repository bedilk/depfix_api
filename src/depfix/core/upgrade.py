"""Assemble a version-window upgrade from already-scanned feed data."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

from depfix.core.models import BreakingChange, ChangeKind, is_method_removal
from depfix.scanners.matching import ScanMatchStatus, assess_scan_change
from depfix.scanners.models import RepoScanResult
from depfix.scanners.removals import RemovalNotice, collect_removal_notices
from depfix.sources.semver import in_upgrade_window, parse_semver


class StepKind(StrEnum):
    SOURCE = "source"
    MANIFEST = "manifest"
    LOCKFILE = "lockfile"


@dataclass(frozen=True)
class UpgradeStep:
    kind: str
    summary: str
    dedupe_key: str = ""
    relpaths: tuple[str, ...] = ()
    origin: str = ""

    def to_dict(self) -> dict:
        return {**self.__dict__, "relpaths": list(self.relpaths)}

    @classmethod
    def from_dict(cls, raw: dict) -> UpgradeStep:
        return cls(**{**raw, "relpaths": tuple(raw.get("relpaths", ()))})


@dataclass
class UpgradePlan:
    provider_id: str
    package: str
    ecosystem: str
    installed_version: str | None
    target_version: str
    drift_change: BreakingChange | None = None
    migrations: list[BreakingChange] = field(default_factory=list)
    removals: list[RemovalNotice] = field(default_factory=list)
    unplaceable: list[BreakingChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        payload = (
            f"upgrade|{self.provider_id}|{self.package}|"
            f"{self.installed_version or '?'}|{self.target_version}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def member_dedupe_keys(self) -> tuple[str, ...]:
        keys = [c.dedupe_key for c in self.migrations]
        if self.drift_change is not None:
            keys.append(self.drift_change.dedupe_key)
        return tuple(keys)

    @property
    def crosses_major(self) -> bool:
        old, new = parse_semver(self.installed_version), parse_semver(self.target_version)
        return bool(old and new and new[0] > old[0])

    @property
    def has_source_work(self) -> bool:
        return bool(self.migrations)

    @property
    def needs_draft(self) -> bool:
        return (
            any(n.blocks_merge for n in self.removals)
            or bool(self.unplaceable)
            or (self.crosses_major and not self.has_source_work)
        )

    @property
    def title(self) -> str:
        span = f"{self.installed_version or '?'} → {self.target_version}"
        if not self.migrations:
            return f"depfix: upgrade {self.package} {span}"
        return (
            f"depfix: upgrade {self.package} {span} "
            f"({len(self.migrations)} API migration"
            f"{'s' if len(self.migrations) > 1 else ''})"
        )


def _installed_version(result: RepoScanResult, package: str) -> str | None:
    for dep in result.dependencies:
        if dep.package != package:
            continue
        candidate = dep.resolved_version or (dep.declared_range or "").lstrip("^~>=< ")
        if parse_semver(candidate):
            return candidate
    return None


def build_upgrade_plan(
    result: RepoScanResult,
    changes: list[BreakingChange],
    *,
    provider_id: str,
    package: str,
    ecosystem: str,
    target_version: str | None = None,
) -> UpgradePlan | None:
    relevant = [c for c in changes if not c.package or c.package == package]
    drift = next((c for c in relevant if c.kind is ChangeKind.DEPENDENCY_VERSION_BUMP), None)

    installed = _installed_version(result, package) or (drift.old_version if drift else None)
    goal = target_version or (drift.new_version if drift else None)
    if goal is None:
        candidates = [
            (parse_semver(c.new_version), c.new_version) for c in relevant if c.new_version
        ]
        placed = [(v, raw) for v, raw in candidates if v is not None]
        goal = max(placed)[1] if placed else None
    if goal is None or parse_semver(goal) is None:
        return None

    plan = UpgradePlan(
        provider_id=provider_id,
        package=package,
        ecosystem=ecosystem,
        installed_version=installed,
        target_version=goal,
        drift_change=drift,
    )

    for change in relevant:
        if change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP:
            continue
        if is_method_removal(change):
            continue
        placed = in_upgrade_window(change.new_version or "", installed, goal)  # type: ignore[assignment]
        if placed is None:
            plan.unplaceable.append(change)
            plan.notes.append(
                f"cannot place {change.old_api} (version {change.new_version!r}) "
                f"in the {installed or '?'} → {goal} window; excluded from this upgrade"
            )
            continue
        if not placed:
            continue
        assessment = assess_scan_change(result, change, provider_id=provider_id)
        if assessment.status is ScanMatchStatus.ACTIONABLE:
            plan.migrations.append(change)
        elif assessment.status is ScanMatchStatus.CURRENT:
            plan.notes.append(f"{change.old_api}: already migrated in this repo")

    plan.removals = collect_removal_notices(
        result, relevant, provider_id=provider_id, package=package, target_version=goal
    )

    if plan.installed_version == goal and not plan.migrations:
        return None
    return plan
