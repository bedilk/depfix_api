"""Scoring functions — pure, no I/O, so they're trivially unit-testable."""

from __future__ import annotations

from dataclasses import dataclass

from depfix.core.models import BreakingChange
from depfix.evals.models import EvalReport, ExpectedCallSite, ExpectedChange, FixAssertion
from depfix.scanners.models import CallSite

# Mirrors Settings.scan_min_recall's default -- kept as a plain literal here
# rather than importing depfix.config, since scoring stays pure/import-light
# and a case-level `min_recall` override is expected to be the common path.
_DEFAULT_MIN_RECALL = 0.85
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def score_classification(
    expected: list[ExpectedChange], actual: list[BreakingChange]
) -> tuple[bool, str]:
    """Greedy one-to-one match: each expected change consumes at most one
    actual change, so a classifier that returns the same change five times
    cannot make five expectations pass."""
    if not expected:
        return True, "no expected changes"

    remaining = list(actual)
    misses: list[str] = []
    for exp in expected:
        match = next(
            (
                a
                for a in remaining
                if a.kind.value in exp.kinds
                and exp.old_api_contains.lower() in a.old_api.lower()
                and a.confidence >= exp.min_confidence
            ),
            None,
        )
        if match is None:
            misses.append(f"kind in {list(exp.kinds)} old_api~={exp.old_api_contains!r}")
        else:
            remaining.remove(match)

    if misses:
        return False, "unmatched expectations: " + "; ".join(misses)
    return True, f"matched {len(expected)}/{len(expected)} expected change(s)"


def score_fix(assertion: FixAssertion, original: str, fixed: str) -> tuple[bool, str]:
    """Property-based check on generated-fix output rather than exact-match,
    which measures formatting luck, not whether the migration is correct."""
    for needle in assertion.must_contain:
        if needle not in fixed:
            return False, f"missing required substring: {needle!r}"
    for needle in assertion.must_not_contain:
        if needle in fixed:
            return False, f"contains forbidden substring: {needle!r}"

    if assertion.max_changed_line_ratio is not None:
        orig_lines = set(original.splitlines())
        fixed_lines = set(fixed.splitlines())
        changed = len(orig_lines.symmetric_difference(fixed_lines))
        denom = max(len(original.splitlines()), 1)
        ratio = changed / denom
        if ratio > assertion.max_changed_line_ratio:
            return False, (
                f"changed-line ratio {ratio:.2f} exceeds max {assertion.max_changed_line_ratio:.2f}"
            )

    return True, "fix assertions satisfied"


def _call_site_matches(exp: ExpectedCallSite, site: CallSite) -> bool:
    if exp.symbol_contains and exp.symbol_contains.lower() not in site.symbol.lower():
        return False
    if exp.kind and site.kind.value != exp.kind:
        return False
    if exp.file_contains and exp.file_contains not in site.filepath:
        return False
    return _CONFIDENCE_RANK[site.confidence.value] >= _CONFIDENCE_RANK[exp.min_confidence]


def score_call_sites(
    expected: list[ExpectedCallSite],
    forbidden_files: list[str],
    call_sites: list[CallSite],
    *,
    min_recall: float | None = None,
) -> tuple[bool, str]:
    """Score one ``call_sites`` case.

    ``forbidden_files`` is an absolute precision gate, checked first: if any
    *actionable* (HIGH/MEDIUM) call site lands in a forbidden file, the case
    fails outright regardless of recall -- a scanner that finds every real
    call site but also flags an unrelated repo's file is not safe to hand to
    a fixer.

    Recall against ``expected`` is then graded against ``min_recall`` (a
    per-case override, falling back to the corpus-wide floor from
    ``docs/plan.md``) rather than requiring every expectation to match --
    below that floor is the documented trigger to bring in tree-sitter
    instead of tightening regexes further.
    """
    actionable_violations = sorted(
        {
            site.filepath
            for site in call_sites
            if site.is_actionable and any(f in site.filepath for f in forbidden_files)
        }
    )
    if actionable_violations:
        return False, "forbidden_files violated by actionable call site(s) in: " + "; ".join(
            actionable_violations
        )

    if not expected:
        return True, "no expected call sites"

    floor = min_recall if min_recall is not None else _DEFAULT_MIN_RECALL
    remaining = list(call_sites)
    misses: list[str] = []
    for exp in expected:
        match = next((s for s in remaining if _call_site_matches(exp, s)), None)
        if match is None:
            misses.append(f"symbol~={exp.symbol_contains!r} kind={exp.kind!r}")
        else:
            remaining.remove(match)

    recall = (len(expected) - len(misses)) / len(expected)
    if recall < floor:
        return False, (
            f"recall {recall:.2f} below floor {floor:.2f}; unmatched: " + "; ".join(misses)
        )
    if misses:
        return True, (
            f"recall {recall:.2f} meets floor {floor:.2f}; unmatched: " + "; ".join(misses)
        )
    return True, f"matched {len(expected)}/{len(expected)} expected call site(s)"


@dataclass(frozen=True)
class Regression:
    case_id: str
    was_passing: bool
    now_passing: bool


def compare_reports(baseline: EvalReport, candidate: EvalReport) -> list[Regression]:
    """Per-case regressions — an *improved* aggregate pass rate can still
    hide one specific case that flipped from pass to fail. Only that
    direction is reported; a fail -> pass flip is never a regression."""
    baseline_by_id = {c.case_id: c.passed for c in baseline.scored_cases}
    regressions: list[Regression] = []
    for case in candidate.scored_cases:
        was = baseline_by_id.get(case.case_id)
        if was is True and not case.passed:
            regressions.append(
                Regression(case_id=case.case_id, was_passing=True, now_passing=False)
            )
    return regressions
