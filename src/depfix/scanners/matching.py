"""Match persisted scan observations to classified upstream changes.

The module is deliberately small at its interface: pass one scan result and
classified changes, receive explainable decisions. It keeps version evidence,
symbol normalization, and confidence filtering out of CLI orchestration.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from depfix.core.models import BreakingChange, ChangeKind
from depfix.scanners.applicability import change_applies
from depfix.scanners.models import CallSite, RepoScanResult
from depfix.scanners.repo import symbols_for_api, symbols_for_change


class ScanMatchStatus(StrEnum):
    """The result of comparing one classified change with one repo scan."""

    ACTIONABLE = "actionable"
    CURRENT = "current"
    NO_CALL_SITES = "no_call_sites"
    VERSION_NOT_AFFECTED = "version_not_affected"


@dataclass(frozen=True)
class ScanChangeAssessment:
    """An explainable decision for one ``(repo scan, breaking change)`` pair."""

    change: BreakingChange
    status: ScanMatchStatus
    reason: str
    matched_sites: tuple[CallSite, ...] = ()

    @property
    def is_actionable(self) -> bool:
        return self.status is ScanMatchStatus.ACTIONABLE


def assess_scan_change(
    result: RepoScanResult,
    change: BreakingChange,
    *,
    provider_id: str | None = None,
) -> ScanChangeAssessment:
    """Decide whether a classified change can be planned for this scan.

    Version evidence is checked first. API migrations then require a
    high/medium-confidence call site matching the change's old symbol. A
    dependency version bump is manifest-level and remains actionable when its
    version evidence applies even without a source-level call site.
    """
    applicability = change_applies(result.dependencies, change)
    if applicability.blocks:
        return ScanChangeAssessment(
            change, ScanMatchStatus.VERSION_NOT_AFFECTED, applicability.reason
        )

    if change.kind is ChangeKind.DEPENDENCY_VERSION_BUMP:
        return ScanChangeAssessment(
            change,
            ScanMatchStatus.ACTIONABLE,
            f"dependency version drift: {applicability.reason}",
        )

    symbols = symbols_for_change(provider_id or change.provider_id, change)
    exact = tuple(s for s in result.actionable_sites if _site_matches_exact(s, symbols))
    if exact:
        return ScanChangeAssessment(
            change,
            ScanMatchStatus.ACTIONABLE,
            f"{len(exact)} actionable call site(s) match the old API (exact/prefix)",
            exact,
        )
    suffix = tuple(s for s in result.actionable_sites if _site_matches_suffix(s, symbols))
    if suffix:
        return ScanChangeAssessment(
            change,
            ScanMatchStatus.ACTIONABLE,
            f"{len(suffix)} call site(s) match the old API by member-path suffix "
            "(an elided accessor; review carefully)",
            suffix,
        )
    # Only symbols that differ from the old API can prove a migration happened;
    # for a same-method param/field change they prove nothing.
    replacement_symbols = tuple(
        s
        for s in symbols_for_api(provider_id or change.provider_id, change.new_api)
        if s not in symbols
    )
    if replacement_symbols and any(
        _site_matches_exact(site, replacement_symbols) for site in result.actionable_sites
    ):
        return ScanChangeAssessment(
            change,
            ScanMatchStatus.CURRENT,
            "repository already uses the feed-documented replacement API",
        )
    return ScanChangeAssessment(
        change,
        ScanMatchStatus.NO_CALL_SITES,
        "no actionable call sites match the old API",
    )


def assess_scan_changes(
    result: RepoScanResult,
    changes: Iterable[BreakingChange],
    *,
    provider_id: str | None = None,
) -> list[ScanChangeAssessment]:
    """Assess every currently classified change for one provider scan."""
    return [assess_scan_change(result, change, provider_id=provider_id) for change in changes]


def _site_matches_exact(site: CallSite, wanted_symbols: tuple[str, ...]) -> bool:
    """Exact or prefix match: wanted is a prefix of observed."""
    return any(
        site.symbol == wanted or site.symbol.startswith(f"{wanted}.") for wanted in wanted_symbols
    )


def _member_suffix_matches(observed: str, wanted: str) -> bool:
    """True if wanted's member path is a contiguous suffix of observed's,
    with the same provider head and identical leaf.

    catalog  ``mongodb.collection.count``  (member: collection.count)
    observed ``mongodb.db.collection.count`` (member: db.collection.count)
    → suffix match, because collection.count is a suffix of db.collection.count
    """
    observed_parts = observed.split(".")
    wanted_parts = wanted.split(".")
    if len(wanted_parts) < 2 or len(observed_parts) <= len(wanted_parts):
        return False
    if observed_parts[0] != wanted_parts[0]:
        return False
    if observed_parts[-1] != wanted_parts[-1]:
        return False
    wanted_member = wanted_parts[1:]
    observed_member = observed_parts[1:]
    return observed_member[-len(wanted_member) :] == wanted_member


def _site_matches_suffix(site: CallSite, wanted_symbols: tuple[str, ...]) -> bool:
    """Suffix match for elided accessor paths (e.g. catalog omits .db)."""
    return any(_member_suffix_matches(site.symbol, wanted) for wanted in wanted_symbols)


# Keep the old name as an alias so existing callers still work.
def _site_matches(site: CallSite, wanted_symbols: tuple[str, ...]) -> bool:
    return _site_matches_exact(site, wanted_symbols) or _site_matches_suffix(site, wanted_symbols)
