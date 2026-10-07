"""Does the repo's own code actually reach the affected API?

A version-only tool (Dependabot) alerts on a vulnerable *version* even when
your code never calls the vulnerable function. depfix already finds call
sites, so it can answer the sharper question: is the changed/vulnerable
symbol actually *reached* by this repository's code? An unreachable finding
is still real -- the dependency is present -- but it is lower priority, and
saying so is a better triage than "you're on a bad version, good luck."

Two tiers, cheap first:
  1. Static (free): does any actionable call site match the affected
     symbol(s)? For an advisory that names affected functions, this is exact.
     For one that names none, we fall back to "is the SDK called at all".
  2. LLM (optional, only when static is ambiguous): ask the model whether the
     matched call sites plausibly exercise the affected code path. This is
     where the LLM earns its cost over a free tool -- but it only ever
     *downgrades* to "reachable/unknown", never dismisses on its own (a human
     rule does that; see classify.triage).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from depfix.core.models import BreakingChange
from depfix.scanners.models import RepoScanResult
from depfix.scanners.repo import symbols_for_api, symbols_for_change


class Reachability(StrEnum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReachabilityVerdict:
    status: Reachability
    reason: str
    matched_symbols: tuple[str, ...] = ()

    @property
    def is_low_priority(self) -> bool:
        return self.status is Reachability.UNREACHABLE


def assess_reachability(
    change: BreakingChange, scan_result: RepoScanResult, *, provider_id: str
) -> ReachabilityVerdict:
    """Static reachability: exact when the change names affected symbols,
    conservative ("unknown") when it doesn't and the SDK is used at all."""
    actionable = scan_result.actionable_sites
    if not actionable:
        return ReachabilityVerdict(
            Reachability.UNREACHABLE,
            "no actionable call sites for this provider in the repository",
        )

    wanted = symbols_for_change(provider_id, change)
    if not wanted:
        return ReachabilityVerdict(
            Reachability.UNKNOWN,
            "the SDK is used, but this change names no specific symbol to match",
        )

    matched = tuple(
        site.symbol
        for site in actionable
        if any(site.symbol == w or site.symbol.startswith(f"{w}.") for w in wanted)
    )
    if matched:
        return ReachabilityVerdict(
            Reachability.REACHABLE,
            f"{len(matched)} call site(s) touch the affected symbol",
            tuple(dict.fromkeys(matched)),
        )
    also_new = symbols_for_api(provider_id, change.new_api)
    if also_new and any(
        site.symbol == n or site.symbol.startswith(f"{n}.") for site in actionable for n in also_new
    ):
        return ReachabilityVerdict(
            Reachability.UNREACHABLE,
            "the repository already uses the replacement/safe API",
        )
    return ReachabilityVerdict(
        Reachability.UNREACHABLE,
        "the SDK is used, but no call site touches the affected symbol",
    )
