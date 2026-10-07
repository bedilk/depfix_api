"""Unit tests for the eval harness: loader validation, scoring, and the runner."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.evals.loader import CorpusError, load_corpus_dir, load_corpus_file
from depfix.evals.models import (
    CaseResult,
    EvalCase,
    EvalCategory,
    EvalReport,
    ExpectedChange,
    FixAssertion,
)
from depfix.evals.runner import EvalRunner, load_report, save_report
from depfix.evals.scoring import compare_reports, score_classification, score_fix

# --- loader -----------------------------------------------------------------


def _write_corpus(tmp_path: Path, name: str, cases: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(cases), encoding="utf-8")
    return path


def test_load_corpus_file_requires_verified_field(tmp_path: Path) -> None:
    path = _write_corpus(tmp_path, "bad.yaml", [{"id": "x", "category": "spec_diff"}])
    with pytest.raises(CorpusError, match="verified"):
        load_corpus_file(path)


def test_load_corpus_file_rejects_unknown_category(tmp_path: Path) -> None:
    path = _write_corpus(
        tmp_path, "bad.yaml", [{"id": "x", "category": "not_a_category", "verified": True}]
    )
    with pytest.raises(CorpusError, match="unknown category"):
        load_corpus_file(path)


def test_load_corpus_file_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = _write_corpus(
        tmp_path,
        "dupe.yaml",
        [
            {"id": "x", "category": "spec_diff", "verified": True},
            {"id": "x", "category": "spec_diff", "verified": True},
        ],
    )
    with pytest.raises(CorpusError, match="duplicate case id"):
        load_corpus_file(path)


def test_load_corpus_file_defaults_requires_llm_by_category(tmp_path: Path) -> None:
    path = _write_corpus(
        tmp_path,
        "cat.yaml",
        [
            {"id": "spec", "category": "spec_diff", "verified": True},
            {"id": "notes", "category": "release_notes", "verified": True},
        ],
    )
    cases = {c.id: c for c in load_corpus_file(path)}
    assert cases["spec"].requires_llm is False
    assert cases["notes"].requires_llm is True


def test_load_corpus_dir_rejects_duplicate_ids_across_files(tmp_path: Path) -> None:
    _write_corpus(tmp_path, "a.yaml", [{"id": "x", "category": "spec_diff", "verified": True}])
    _write_corpus(tmp_path, "b.yaml", [{"id": "x", "category": "spec_diff", "verified": True}])
    with pytest.raises(CorpusError, match="duplicate case id"):
        load_corpus_dir(tmp_path)


def test_load_corpus_dir_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="does not exist"):
        load_corpus_dir(tmp_path / "nope")


def test_load_corpus_dir_loads_real_shipped_corpus() -> None:
    corpus_dir = Path(__file__).resolve().parents[2] / "src" / "depfix" / "evals" / "corpus"
    cases = load_corpus_dir(corpus_dir)
    assert len(cases) > 0
    assert all(isinstance(c.verified, bool) for c in cases)


def test_eval_summary_distinguishes_scored_and_skipped_cases() -> None:
    report = EvalReport(cases=[_result("pass", True), _result("skip", True, skipped=True)])

    assert report.summary_line == "pass rate: 100% (1/1 scored), 1 skipped of 2 total"


# --- scoring: classification --------------------------------------------------


def _bc(
    kind: ChangeKind = ChangeKind.METHOD_REMOVED, old_api: str = "foo.bar", confidence: float = 1.0
) -> BreakingChange:
    return BreakingChange(
        package="pkg",
        old_version="1.0",
        new_version="2.0",
        old_api=old_api,
        new_api="",
        description="d",
        migration_guide="",
        kind=kind,
        source=ClassificationSource.SPEC_DIFF,
        confidence=confidence,
    )


def test_score_classification_no_expectations_always_passes() -> None:
    passed, _ = score_classification([], [])
    assert passed is True


def test_score_classification_matches_by_kind_and_substring() -> None:
    expected = [ExpectedChange(kind="method_removed", old_api_contains="foo")]
    actual = [_bc(old_api="foo.bar")]
    passed, detail = score_classification(expected, actual)
    assert passed is True
    assert "matched 1/1" in detail


def test_score_classification_fails_on_missing_expectation() -> None:
    expected = [ExpectedChange(kind="method_removed", old_api_contains="baz")]
    actual = [_bc(old_api="foo.bar")]
    passed, detail = score_classification(expected, actual)
    assert passed is False
    assert "unmatched expectations" in detail


def test_score_classification_respects_min_confidence() -> None:
    expected = [ExpectedChange(kind="method_removed", old_api_contains="foo", min_confidence=0.9)]
    actual = [_bc(old_api="foo.bar", confidence=0.5)]
    passed, _ = score_classification(expected, actual)
    assert passed is False


def test_score_classification_is_greedy_one_to_one() -> None:
    """Two identical expectations shouldn't both be satisfied by one actual change."""
    expected = [
        ExpectedChange(kind="method_removed", old_api_contains="foo"),
        ExpectedChange(kind="method_removed", old_api_contains="foo"),
    ]
    actual = [_bc(old_api="foo.bar")]
    passed, _ = score_classification(expected, actual)
    assert passed is False


# --- scoring: fix generation --------------------------------------------------


def test_score_fix_must_contain() -> None:
    assertion = FixAssertion(must_contain=["chat.completions.create"])
    passed, _ = score_fix(assertion, "old", "openai.chat.completions.create()")
    assert passed is True


def test_score_fix_must_not_contain() -> None:
    assertion = FixAssertion(must_not_contain=["createChatCompletion"])
    passed, detail = score_fix(assertion, "old", "openai.createChatCompletion()")
    assert passed is False
    assert "forbidden substring" in detail


def test_score_fix_max_changed_line_ratio() -> None:
    original = "a\nb\nc\nd\n"
    fixed = "a\nb\nc\nZZZ\n"
    assertion = FixAssertion(max_changed_line_ratio=0.1)
    passed, detail = score_fix(assertion, original, fixed)
    assert passed is False
    assert "changed-line ratio" in detail


# --- compare_reports -----------------------------------------------------------


def _result(case_id: str, passed: bool, skipped: bool = False) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        category=EvalCategory.SPEC_DIFF,
        passed=passed,
        verified=True,
        skipped=skipped,
    )


def test_compare_reports_flags_pass_to_fail_regression() -> None:
    baseline = EvalReport(cases=[_result("a", True)])
    candidate = EvalReport(cases=[_result("a", False)])
    regressions = compare_reports(baseline, candidate)
    assert len(regressions) == 1
    assert regressions[0].case_id == "a"


def test_compare_reports_ignores_fail_to_pass() -> None:
    baseline = EvalReport(cases=[_result("a", False)])
    candidate = EvalReport(cases=[_result("a", True)])
    assert compare_reports(baseline, candidate) == []


def test_compare_reports_ignores_skipped_cases() -> None:
    baseline = EvalReport(cases=[_result("a", True)])
    candidate = EvalReport(cases=[_result("a", False, skipped=True)])
    assert compare_reports(baseline, candidate) == []


# --- report round-trip ---------------------------------------------------------


def test_save_and_load_report_round_trips(tmp_path: Path) -> None:
    report = EvalReport(
        cases=[
            CaseResult(
                case_id="x",
                category=EvalCategory.FIX_GENERATION,
                passed=True,
                verified=False,
                detail="ok",
                cost=0.01,
                skipped=False,
            )
        ]
    )
    path = tmp_path / "report.json"
    save_report(report, path)
    loaded = load_report(path)
    assert len(loaded.cases) == 1
    assert loaded.cases[0].case_id == "x"
    assert loaded.cases[0].category is EvalCategory.FIX_GENERATION
    assert loaded.cases[0].cost == 0.01


# --- EvalRunner: deterministic spec_diff path (no LLM needed) -----------------


def test_eval_runner_spec_diff_case_passes_without_llm() -> None:
    corpus_dir = Path(__file__).resolve().parents[2] / "src" / "depfix" / "evals" / "corpus"
    cases = [c for c in load_corpus_dir(corpus_dir) if c.category is EvalCategory.SPEC_DIFF]
    assert cases, "expected at least one spec_diff case in the shipped corpus"

    runner = EvalRunner()
    report = runner.run(cases, use_llm=False)

    assert report.scored_cases
    assert all(not c.skipped for c in report.cases)
    assert report.pass_rate >= 0.80


def test_eval_runner_skips_llm_cases_when_use_llm_false() -> None:
    corpus_dir = Path(__file__).resolve().parents[2] / "src" / "depfix" / "evals" / "corpus"
    cases = [c for c in load_corpus_dir(corpus_dir) if c.requires_llm]
    assert cases, "expected at least one LLM-requiring case in the shipped corpus"

    runner = EvalRunner()
    report = runner.run(cases, use_llm=False)

    assert all(c.skipped for c in report.cases)
    assert report.scored_cases == []
    assert report.pass_rate == 1.0


# --- EvalRunner: call_sites min_recall wiring (Settings.scan_min_recall) ------


def _call_sites_case(*, min_recall: float | None) -> EvalCase:
    return EvalCase(
        id="cs-1",
        category=EvalCategory.CALL_SITES,
        verified=True,
        fixture="cs-fixture",
        min_recall=min_recall,
    )


def test_eval_runner_call_sites_uses_case_min_recall_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case's own ``min_recall`` overrides ``default_min_recall`` --
    `Settings.scan_min_recall` is only the corpus-wide fallback."""
    (tmp_path / "cs-fixture").mkdir()
    seen: dict[str, float | None] = {}

    def fake_score_call_sites(expected, forbidden_files, call_sites, *, min_recall=None):
        seen["min_recall"] = min_recall
        return True, "stubbed"

    monkeypatch.setattr("depfix.evals.runner.score_call_sites", fake_score_call_sites)

    runner = EvalRunner(fixtures_dir=tmp_path, default_min_recall=0.5)
    runner._run_call_sites(_call_sites_case(min_recall=0.99))

    assert seen["min_recall"] == 0.99


def test_eval_runner_call_sites_falls_back_to_default_min_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-case override -- the harness's floor is whatever
    ``Settings.scan_min_recall`` the CLI passed in as ``default_min_recall``,
    not a second hardcoded literal."""
    (tmp_path / "cs-fixture").mkdir()
    seen: dict[str, float | None] = {}

    def fake_score_call_sites(expected, forbidden_files, call_sites, *, min_recall=None):
        seen["min_recall"] = min_recall
        return True, "stubbed"

    monkeypatch.setattr("depfix.evals.runner.score_call_sites", fake_score_call_sites)

    runner = EvalRunner(fixtures_dir=tmp_path, default_min_recall=0.5)
    runner._run_call_sites(_call_sites_case(min_recall=None))

    assert seen["min_recall"] == 0.5
