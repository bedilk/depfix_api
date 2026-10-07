"""Turn a detected change into structured, actionable ``BreakingChange`` rows."""

from depfix.classify.classifier import Classifier
from depfix.classify.llm import GeminiCompleter, LLMCompleter, LLMResponse, OllamaCompleter
from depfix.classify.models import ClassifyOutcome
from depfix.classify.notes import NotesClassifier
from depfix.classify.spec_rules import classify_spec_changes, sdk_path_hints
from depfix.classify.store import (
    ClassifyRunSummary,
    breaking_change_from_row,
    classify_pending,
    event_from_row,
    load_unclassified_events,
    persist_classification,
)
from depfix.classify.usage_candidates import (
    UsageCandidateResolver,
    discover_usage_candidates,
)

__all__ = [
    "Classifier",
    "ClassifyOutcome",
    "ClassifyRunSummary",
    "GeminiCompleter",
    "LLMCompleter",
    "LLMResponse",
    "NotesClassifier",
    "OllamaCompleter",
    "UsageCandidateResolver",
    "breaking_change_from_row",
    "classify_pending",
    "classify_spec_changes",
    "discover_usage_candidates",
    "event_from_row",
    "load_unclassified_events",
    "persist_classification",
    "sdk_path_hints",
]
