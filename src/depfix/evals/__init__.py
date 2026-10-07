"""Eval harness for scoring the classify/fixer pipeline against a YAML corpus."""

from depfix.evals.loader import CorpusError, load_corpus_dir, load_corpus_file
from depfix.evals.models import (
    CaseResult,
    EvalCase,
    EvalCategory,
    EvalReport,
    ExpectedCallSite,
    ExpectedChange,
    FixAssertion,
)
from depfix.evals.runner import EvalRunner, load_report, save_report
from depfix.evals.scoring import (
    Regression,
    compare_reports,
    score_call_sites,
    score_classification,
    score_fix,
)

__all__ = [
    "CaseResult",
    "CorpusError",
    "EvalCase",
    "EvalCategory",
    "EvalReport",
    "EvalRunner",
    "ExpectedCallSite",
    "ExpectedChange",
    "FixAssertion",
    "Regression",
    "compare_reports",
    "load_corpus_dir",
    "load_corpus_file",
    "load_report",
    "save_report",
    "score_call_sites",
    "score_classification",
    "score_fix",
]
