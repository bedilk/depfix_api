"""Value objects for the eval harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvalCategory(StrEnum):
    """Which pipeline stage a case exercises."""

    SPEC_DIFF = "spec_diff"  # deterministic — diff_specs() + classify_spec_changes()
    RELEASE_NOTES = "release_notes"  # LLM — NotesClassifier
    FIX_GENERATION = "fix_generation"  # LLM — FixGenerator/OllamaFixGenerator + FixValidator
    CALL_SITES = "call_sites"  # deterministic — CallSiteScanner, Week 3


@dataclass
class ExpectedChange:
    """What a classification case expects to see, matched loosely (kind +
    a substring of old_api) rather than by exact equality — exact-match on
    LLM output measures formatting luck, not correctness.

    ``also_accept`` exists for genuinely ambiguous taxonomy calls: "the
    parameter is no longer accepted, use this other one instead" is
    defensibly ``param_removed`` or ``param_renamed``, and failing a case
    on that coin flip measures our labelling convention, not the
    classifier. It deliberately does not loosen ``old_api_contains`` or
    ``min_confidence``, so a wrong *subject* still fails.
    """

    kind: str
    old_api_contains: str = ""
    min_confidence: float = 0.0
    also_accept: tuple[str, ...] = ()

    @property
    def kinds(self) -> tuple[str, ...]:
        return (self.kind, *self.also_accept)


@dataclass
class FixAssertion:
    """Property-based assertions on generated-fix output. Same rationale as
    ``ExpectedChange``: exact-match on LLM code output measures formatting
    luck, not correctness."""

    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    max_changed_line_ratio: float | None = None


@dataclass
class ExpectedCallSite:
    """What a ``call_sites`` case expects to find, matched loosely (substrings
    + a minimum confidence floor) for the same reason ``ExpectedChange``
    is loose: exact-match on a scanner's exact line/column measures
    incidental formatting, not "did it find the call"."""

    symbol_contains: str = ""
    kind: str = ""  # a CallSiteKind value, or "" to match any kind
    file_contains: str = ""
    min_confidence: str = "low"  # "low" | "medium" | "high"


@dataclass
class EvalCase:
    """One corpus entry. ``verified`` is mandatory at load time (see
    ``loader.py``) — there is no default, on purpose."""

    id: str
    category: EvalCategory
    verified: bool
    description: str = ""
    requires_llm: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    # spec_diff
    package: str = ""
    old_version: str = ""
    new_version: str = ""
    old_spec: dict[str, Any] | None = None
    new_spec: dict[str, Any] | None = None
    expected_changes: list[ExpectedChange] = field(default_factory=list)

    # release_notes
    release_notes_body: str = ""

    # fix_generation
    breaking_change: dict[str, Any] | None = None
    fixture: str = ""
    target_file: str = ""
    fix_assertion: FixAssertion | None = None

    # call_sites (Week 3) -- `fixture` above is reused as the checkout dir
    provider_id: str = ""
    sdk_packages: list[str] = field(default_factory=list)
    api_base_urls: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    expected_call_sites: list[ExpectedCallSite] = field(default_factory=list)
    forbidden_files: list[str] = field(default_factory=list)
    min_recall: float | None = None


@dataclass
class CaseResult:
    case_id: str
    category: EvalCategory
    passed: bool
    verified: bool
    detail: str = ""
    cost: float = 0.0
    skipped: bool = False
    duration_ms: int = 0


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def scored_cases(self) -> list[CaseResult]:
        """Cases that actually ran — skipped (no-LLM) cases don't count
        toward the pass rate in either direction."""
        return [c for c in self.cases if not c.skipped]

    @property
    def skipped_cases(self) -> list[CaseResult]:
        return [c for c in self.cases if c.skipped]

    @property
    def pass_rate(self) -> float:
        scored = self.scored_cases
        if not scored:
            return 1.0
        return sum(1 for c in scored if c.passed) / len(scored)

    @property
    def summary_line(self) -> str:
        scored = self.scored_cases
        skipped = self.skipped_cases
        text = f"pass rate: {self.pass_rate:.0%} ({sum(case.passed for case in scored)}/{len(scored)} scored)"
        if skipped:
            text += f", {len(skipped)} skipped of {len(self.cases)} total"
        return text

    @property
    def total_cost(self) -> float:
        return sum(c.cost for c in self.cases)

    @property
    def failures(self) -> list[CaseResult]:
        return [c for c in self.scored_cases if not c.passed]

    def by_category(self, category: EvalCategory) -> list[CaseResult]:
        return [c for c in self.cases if c.category is category]
