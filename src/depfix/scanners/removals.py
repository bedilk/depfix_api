"""Deterministic 'removed API still called here' findings."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from depfix.core.models import BreakingChange, is_method_removal
from depfix.scanners.models import CallSite, CallSiteKind, RepoScanResult
from depfix.scanners.repo import symbols_for_change
from depfix.sources.semver import parse_semver


class RemovalImpact(StrEnum):
    BREAKS_ON_UPGRADE = "breaks_on_upgrade"
    ALREADY_REMOVED = "already_removed"
    UPCOMING = "upcoming"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class RemovalNotice:
    change_dedupe_key: str
    package: str
    old_api: str
    removed_in: str
    impact: str
    sites: tuple[str, ...] = ()
    match: str = "exact"
    guidance: str = ""
    evidence: str | None = None
    source_url: str | None = None

    @property
    def reachable(self) -> bool:
        return bool(self.sites)

    @property
    def blocks_merge(self) -> bool:
        return self.reachable and self.impact in (
            RemovalImpact.BREAKS_ON_UPGRADE,
            RemovalImpact.ALREADY_REMOVED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "sites": list(self.sites)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RemovalNotice:
        return cls(**{**raw, "sites": tuple(raw.get("sites", ()))})


def _installed_version(result: RepoScanResult, package: str | None) -> str | None:
    for dep in result.dependencies:
        if package and dep.package != package:
            continue
        candidate = dep.resolved_version or (dep.declared_range or "").lstrip("^~>=< ")
        if parse_semver(candidate):
            return candidate
    return None


def _impact(installed: str | None, removed_in: str, target: str | None) -> RemovalImpact:
    removed = parse_semver(removed_in)
    if removed is None:
        return RemovalImpact.BREAKS_ON_UPGRADE
    inst = parse_semver(installed)
    if inst is not None and inst >= removed:
        return RemovalImpact.ALREADY_REMOVED
    tgt = parse_semver(target)
    if tgt is None or tgt >= removed:
        return RemovalImpact.BREAKS_ON_UPGRADE
    return RemovalImpact.UPCOMING


def _matches_any(symbol: str, wanted: tuple[str, ...]) -> bool:
    return any(symbol == s or symbol.startswith(f"{s}.") for s in wanted)


def collect_removal_notices(
    result: RepoScanResult,
    changes: list[BreakingChange],
    *,
    provider_id: str,
    package: str | None = None,
    target_version: str | None = None,
    include_unreachable: bool = False,
) -> list[RemovalNotice]:
    installed = _installed_version(result, package)
    notices: dict[str, RemovalNotice] = {}
    for change in changes:
        if not is_method_removal(change) or change.dedupe_key in notices:
            continue
        if package and change.package and change.package != package:
            continue
        wanted = symbols_for_change(provider_id or change.provider_id or "", change)
        if not wanted:
            continue
        exact = [s for s in result.actionable_sites if _matches_any(s.symbol, wanted)]
        suffix = (
            []
            if exact
            else [
                s
                for s in result.actionable_sites
                if any(s.symbol.endswith(f".{w.split('.')[-1]}") for w in wanted)
            ]
        )
        sites = exact or suffix
        impact = (
            _impact(installed, change.new_version or "", target_version)
            if sites
            else RemovalImpact.UNREACHABLE
        )
        if impact is RemovalImpact.UNREACHABLE and not include_unreachable:
            continue
        notices[change.dedupe_key] = RemovalNotice(
            change_dedupe_key=change.dedupe_key,
            package=change.package or "",
            old_api=change.old_api,
            removed_in=change.new_version or "",
            impact=impact.value,
            sites=tuple(dict.fromkeys(f"{s.filepath}:{s.line_number}" for s in sites)),
            match="exact" if exact else "suffix",
            guidance=(change.new_api or change.migration_guide or change.description or "").strip(),
            evidence=change.evidence,
            source_url=change.source_url,
        )
    order = {i.value: n for n, i in enumerate(RemovalImpact)}
    return sorted(notices.values(), key=lambda n: (order[n.impact], n.old_api))


def narrow_to_symbols(result: RepoScanResult, symbols: tuple[str, ...]) -> RepoScanResult:
    """Filter call sites to those matching the given symbols (METHOD_CALL/BARE_SYMBOL only)."""
    if not symbols:
        return result
    _FILTERED = frozenset({CallSiteKind.METHOD_CALL, CallSiteKind.BARE_SYMBOL})

    def keep(site: CallSite) -> bool:
        return site.kind not in _FILTERED or _matches_any(site.symbol, symbols)

    return dataclasses.replace(result, call_sites=[s for s in result.call_sites if keep(s)])
