"""Standing triage rules: a repository's instructions about classes of change.

Dependabot's auto-triage lets a repo say "dismiss this kind of alert" without
a human touching each one. depfix already has the raw material -- the
``ignore`` list and the ``.out-of-scope`` idea -- but no structured,
condition-based rule. This module adds one, with a firm constraint drawn from
the triage skill: **an LLM may propose a rule, but only a human confirms it.**
Nothing here dismisses a change on the model's say-so; a rule exists in the
repo config (a human wrote or approved it) before it can suppress anything.

Three actions, mirroring Dependabot:
  "dismiss" -- never act on matching changes (a permanent, reasoned no)
  "snooze"  -- skip until a condition holds (e.g. a fix version is published)
  "proceed" -- explicitly allow (overrides a broader dismiss)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from depfix.core.models import BreakingChange, ChangeKind
from depfix.repoconfig.models import glob_match

_SEVERITY_RANK = {
    "critical": 4,
    "high": 3,
    "moderate": 2,
    "medium": 2,
    "low": 1,
}


class TriageAction(StrEnum):
    DISMISS = "dismiss"
    SNOOZE = "snooze"
    PROCEED = "proceed"


@dataclass(frozen=True)
class TriageRule:
    """One human-approved rule. ``match`` fields are ANDed; empty fields
    match anything. ``reason`` is required for a dismiss -- an unexplained
    permanent suppression is exactly what this system exists to prevent."""

    action: TriageAction
    reason: str = ""
    provider: str = ""
    package: str = ""
    kind: str = ""
    max_severity: str = ""
    until_fix_available: bool = False

    def matches(self, change: BreakingChange) -> bool:
        if self.provider and not glob_match(change.provider_id, self.provider):
            return False
        if self.package and not glob_match(change.package, self.package):
            return False
        if self.kind and change.kind.value != self.kind:
            return False
        if change.kind is ChangeKind.SECURITY_ADVISORY and self.action is TriageAction.DISMISS:
            return self._severity_within_cap(change)
        return True

    def _severity_within_cap(self, change: BreakingChange) -> bool:
        """A dismiss rule for a security advisory only applies up to the
        severity the human explicitly named. Without a cap, or above it, the
        rule does not match -- the advisory proceeds to a human. An advisory
        whose severity we could not determine also proceeds: an unknown
        severity is not a low one.
        """
        if not self.max_severity or change.advisory_severity is None:
            return False
        cap_rank = _SEVERITY_RANK.get(self.max_severity.lower(), 0)
        change_rank = _SEVERITY_RANK.get(change.advisory_severity.lower(), 99)
        return change_rank <= cap_rank

    def is_active(self, change: BreakingChange) -> bool:
        """Whether a snooze still applies. A snooze whose condition has been
        met stops suppressing and the change proceeds normally."""
        if self.action is not TriageAction.SNOOZE:
            return True
        if self.until_fix_available:
            return not change.new_api.strip()
        return True


@dataclass(frozen=True)
class TriageVerdict:
    action: TriageAction
    rule: TriageRule
    reason: str


def evaluate_triage(
    change: BreakingChange, rules: tuple[TriageRule, ...] | tuple
) -> TriageVerdict | None:
    """First matching rule wins (rules are ordered, most-specific first by
    convention). ``None`` means no rule applied -- proceed normally.

    A PROCEED rule that matches short-circuits to "allow", so a repo can
    carve an exception out of a broader dismiss by putting the PROCEED
    rule first.
    """
    for rule in rules:
        if not isinstance(rule, TriageRule):
            continue
        if rule.matches(change) and rule.is_active(change):
            return TriageVerdict(
                action=rule.action,
                rule=rule,
                reason=rule.reason or f"matched {rule.action.value} rule",
            )
    return None


def suggest_triage_rule(change: BreakingChange, reachability_reason: str) -> str:
    """Draft a .depfix.yml triage rule for a human to review and paste.

    Deliberately not an LLM call and deliberately side-effect-free: the rule
    that actually suppresses anything must be written into the repo's own
    config by a person. This only formats the suggestion. If an LLM-written
    justification is wanted later, it belongs in the *reason* text, never in
    the decision to suppress.
    """
    return (
        f"# Suggested -- review before adding. {reachability_reason}\n"
        f"triage_rules:\n"
        f"  - action: dismiss\n"
        f'    package: "{change.package}"\n'
        f'    kind: "{change.kind.value}"\n'
        f'    reason: "unreachable: {reachability_reason}"\n'
    )
