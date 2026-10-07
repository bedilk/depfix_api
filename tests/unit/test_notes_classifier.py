"""Unit tests for LLM-backed release-notes classification (mocked completer)."""

from __future__ import annotations

import json

from depfix.classify.llm import LLMResponse
from depfix.classify.notes import NotesClassifier
from depfix.core.models import ChangeKind, ClassificationSource

RELEASE_NOTES = """## v4.0.0

### Breaking Changes
- Removed `openai.createChatCompletion()`. Use
  `openai.chat.completions.create()` instead.
"""


class _FakeCompleter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(text=self.text)


def _candidate(**overrides: object) -> dict:
    base = {
        "kind": "method_renamed",
        "old_api": "openai.createChatCompletion()",
        "new_api": "openai.chat.completions.create()",
        "description": "Method renamed.",
        "evidence": "Removed `openai.createChatCompletion()`",
    }
    base.update(overrides)
    return base


def test_classify_accepts_change_with_verbatim_evidence() -> None:
    completer = _FakeCompleter(json.dumps([_candidate()]))
    classifier = NotesClassifier(completer)

    changes = classifier.classify(
        RELEASE_NOTES,
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert len(changes) == 1
    change = changes[0]
    assert change.kind is ChangeKind.METHOD_RENAMED
    assert change.source is ClassificationSource.RELEASE_NOTES
    assert change.confidence == 0.75
    assert "createChatCompletion" in change.evidence


def test_classify_drops_change_with_fabricated_evidence() -> None:
    candidate = _candidate(evidence="this text does not appear in the notes")
    completer = _FakeCompleter(json.dumps([candidate]))
    classifier = NotesClassifier(completer)

    changes = classifier.classify(
        RELEASE_NOTES,
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert changes == []


def test_classify_drops_change_below_min_confidence() -> None:
    completer = _FakeCompleter(json.dumps([_candidate()]))
    classifier = NotesClassifier(completer, min_confidence=0.9)

    changes = classifier.classify(
        RELEASE_NOTES,
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert changes == []


def test_classify_ignores_candidate_missing_old_api() -> None:
    candidate = _candidate(old_api="")
    completer = _FakeCompleter(json.dumps([candidate]))
    classifier = NotesClassifier(completer)

    changes = classifier.classify(
        RELEASE_NOTES,
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert changes == []


def test_classify_falls_back_to_unknown_kind_for_invalid_value() -> None:
    candidate = _candidate(kind="not_a_real_kind")
    completer = _FakeCompleter(json.dumps([candidate]))
    classifier = NotesClassifier(completer)

    changes = classifier.classify(
        RELEASE_NOTES,
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.UNKNOWN


def test_classify_handles_markdown_fenced_json() -> None:
    fenced = "```json\n" + json.dumps([_candidate()]) + "\n```"
    completer = _FakeCompleter(fenced)
    classifier = NotesClassifier(completer)

    changes = classifier.classify(
        RELEASE_NOTES,
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert len(changes) == 1


def test_classify_returns_empty_for_blank_body() -> None:
    completer = _FakeCompleter(json.dumps([_candidate()]))
    classifier = NotesClassifier(completer)

    changes = classifier.classify(
        "   ",
        package="openai",
        old_version="3.4.0",
        new_version="4.0.0",
        provider_id="openai",
    )

    assert changes == []
    assert completer.prompts == []
