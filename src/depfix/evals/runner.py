"""Executes eval cases against the real classify/fixer pipeline.

Deliberately bypasses ``DependencyFixAgent`` for the ``fix_generation``
category and calls ``FixGenerator``/``FixValidator`` directly: the agent
orchestrates a whole-codebase run (scan -> fix every file -> write diffs),
whereas an eval case is "does this one fixture file get fixed correctly" —
the agent's file-discovery and diff-writing steps would just be overhead and
extra failure surface unrelated to what's being scored.

``spec_diff`` and ``release_notes`` cases, by contrast, both run through the
same :class:`~depfix.classify.classifier.Classifier` that ``depfix classify``
uses in production — it already branches on whether the built ``ChangeEvent``
carries structural spec deltas (deterministic path) or only prose (LLM path),
so scoring them through two separate methods here would just be a second copy
of that branch to keep in sync.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from depfix.classify.classifier import Classifier
from depfix.classify.llm import LLMCompleter
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.evals.models import CaseResult, EvalCase, EvalCategory, EvalReport
from depfix.evals.scoring import score_call_sites, score_classification, score_fix
from depfix.scanners.base import CodebaseScanner, build_pattern_for_api
from depfix.scanners.callsites import CallSiteScanner
from depfix.scanners.models import ScanTarget
from depfix.sources.models import ChangeEvent, SourceKind
from depfix.sources.openapi_diff import diff_specs
from depfix.validators.javascript import FixValidator

logger = logging.getLogger(__name__)


class FixGeneratorLike(Protocol):
    def generate_fix(self, file_usage: Any, breaking_change: BreakingChange) -> Any: ...


def _breaking_change_from_dict(data: dict[str, Any]) -> BreakingChange:
    payload = dict(data)
    if "kind" in payload:
        payload["kind"] = ChangeKind(payload["kind"])
    if "source" in payload:
        payload["source"] = ClassificationSource(payload["source"])
    return BreakingChange(**payload)


class EvalRunner:
    def __init__(
        self,
        *,
        completer: LLMCompleter | None = None,
        fixer: FixGeneratorLike | None = None,
        fixtures_dir: Path | None = None,
        default_min_recall: float | None = None,
    ) -> None:
        self._completer = completer
        self._classifier = Classifier(completer)
        self._fixer = fixer
        self._fixtures_dir = fixtures_dir or (
            Path(__file__).resolve().parents[3] / "tests" / "fixtures"
        )
        self._scanner = CodebaseScanner(context_lines=5)
        self._call_site_scanner = CallSiteScanner()
        self._validator = FixValidator()
        # Corpus-wide floor for `call_sites` cases that don't set their own
        # `min_recall` -- callers pass `Settings.scan_min_recall` here so the
        # eval harness's floor and the production config it's gating stay
        # one value, not two to keep in sync by hand.
        self._default_min_recall = default_min_recall

    def run(self, cases: list[EvalCase], *, use_llm: bool) -> EvalReport:
        report = EvalReport()
        for case in cases:
            if case.requires_llm and not use_llm:
                report.cases.append(
                    CaseResult(
                        case_id=case.id,
                        category=case.category,
                        passed=False,
                        verified=case.verified,
                        detail="skipped: requires an LLM and --no-llm was set",
                        skipped=True,
                    )
                )
                continue
            report.cases.append(self._run_case(case))
        return report

    def _run_case(self, case: EvalCase) -> CaseResult:
        started = time.time()
        try:
            if case.category in (EvalCategory.SPEC_DIFF, EvalCategory.RELEASE_NOTES):
                result = self._run_classification(case)
            elif case.category is EvalCategory.FIX_GENERATION:
                result = self._run_fix(case)
            elif case.category is EvalCategory.CALL_SITES:
                result = self._run_call_sites(case)
            else:
                raise AssertionError(  # pragma: no cover
                    f"unhandled eval category: {case.category}"
                )
        except Exception as exc:
            logger.exception("eval case %s raised", case.id)
            result = CaseResult(
                case_id=case.id,
                category=case.category,
                passed=False,
                verified=case.verified,
                detail=f"raised {type(exc).__name__}: {exc}",
            )
        result.duration_ms = int((time.time() - started) * 1000)
        return result

    def _run_classification(self, case: EvalCase) -> CaseResult:
        """Score a ``spec_diff`` or ``release_notes`` case through the real
        ``Classifier`` — same entry point ``depfix classify`` uses, so a case
        that only passes here and not in production is measuring the harness,
        not the pipeline."""
        event = ChangeEvent(
            provider_id=case.package,
            feed_key=f"eval:{case.id}",
            source_kind=SourceKind.OPENAPI_SPEC
            if case.category is EvalCategory.SPEC_DIFF
            else SourceKind.GITHUB_RELEASE,
            source_url=None,
            old_token=case.old_version,
            new_token=case.new_version,
            body=case.release_notes_body,
            spec_changes=diff_specs(case.old_spec or {}, case.new_spec or {}),
        )
        outcome = self._classifier.classify(event)
        if outcome.errors:
            return CaseResult(
                case.id,
                case.category,
                False,
                case.verified,
                "; ".join(outcome.errors),
                cost=outcome.cost,
            )
        passed, detail = score_classification(case.expected_changes, outcome.changes)
        return CaseResult(case.id, case.category, passed, case.verified, detail, cost=outcome.cost)

    def _run_fix(self, case: EvalCase) -> CaseResult:
        if self._fixer is None:
            return CaseResult(case.id, case.category, False, case.verified, "no fixer configured")
        if not case.breaking_change or not case.fixture or not case.target_file:
            return CaseResult(
                case.id,
                case.category,
                False,
                case.verified,
                "case is missing breaking_change/fixture/target_file",
            )

        change = _breaking_change_from_dict(case.breaking_change)
        fixture_dir = self._fixtures_dir / case.fixture
        target_path = fixture_dir / case.target_file
        if not target_path.exists():
            return CaseResult(
                case.id, case.category, False, case.verified, f"fixture not found: {target_path}"
            )

        patterns = build_pattern_for_api(change.old_api, change.package)
        file_usages = self._scanner.scan_codebase(str(fixture_dir), patterns)
        file_usage = next(
            (fu for fu in file_usages if fu.filepath.endswith(case.target_file)), None
        )
        if file_usage is None:
            return CaseResult(
                case.id,
                case.category,
                False,
                case.verified,
                "scanner found no usages of old_api in target_file",
            )

        fixed_code, _confidence, llm_call = self._fixer.generate_fix(file_usage, change)
        validation = self._validator.validate(
            file_usage.file_content, fixed_code, file_usage.filepath
        )
        cost = llm_call.cost_estimate

        if not validation.is_valid:
            return CaseResult(
                case.id,
                case.category,
                False,
                case.verified,
                f"validation failed: {validation.error_message}",
                cost=cost,
            )

        if case.fix_assertion is None:
            return CaseResult(
                case.id, case.category, True, case.verified, "no fix assertions", cost=cost
            )

        passed, detail = score_fix(case.fix_assertion, file_usage.file_content, fixed_code)
        return CaseResult(case.id, case.category, passed, case.verified, detail, cost=cost)

    def _run_call_sites(self, case: EvalCase) -> CaseResult:
        """Score a ``call_sites`` case through the real ``CallSiteScanner``,
        against ``case.fixture`` -- a checkout-shaped directory under
        ``tests/fixtures/``, same convention as ``fix_generation``'s
        ``fixture``, just scanned as a whole tree instead of one file."""
        if not case.fixture:
            return CaseResult(
                case.id, case.category, False, case.verified, "case is missing fixture"
            )

        fixture_dir = self._fixtures_dir / case.fixture
        if not fixture_dir.exists():
            return CaseResult(
                case.id, case.category, False, case.verified, f"fixture not found: {fixture_dir}"
            )

        target = ScanTarget(
            provider_id=case.provider_id,
            sdk_packages=tuple(case.sdk_packages),
            api_base_urls=tuple(case.api_base_urls),
            symbols=tuple(case.symbols),
        )
        result = self._call_site_scanner.scan(fixture_dir, target)
        passed, detail = score_call_sites(
            case.expected_call_sites,
            case.forbidden_files,
            result.call_sites,
            min_recall=case.min_recall if case.min_recall is not None else self._default_min_recall,
        )
        return CaseResult(case.id, case.category, passed, case.verified, detail)


def save_report(report: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{**asdict(c), "category": c.category.value} for c in report.cases]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_report(path: Path) -> EvalReport:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        CaseResult(
            case_id=item["case_id"],
            category=EvalCategory(item["category"]),
            passed=item["passed"],
            verified=item["verified"],
            detail=item.get("detail", ""),
            cost=item.get("cost", 0.0),
            skipped=item.get("skipped", False),
            duration_ms=item.get("duration_ms", 0),
        )
        for item in raw
    ]
    return EvalReport(cases=cases)
