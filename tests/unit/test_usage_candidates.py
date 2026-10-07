"""Tests for feed-grounded, repository-aware migration discovery."""

import json

from depfix.classify.llm import LLMResponse
from depfix.classify.usage_candidates import UsageCandidateResolver
from depfix.core.models import ChangeKind, ClassificationSource
from depfix.sources.models import ChangeEvent, SourceKind


class _FakeCompleter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(text=self.text)


def _event() -> ChangeEvent:
    return ChangeEvent(
        provider_id="openai",
        feed_key="github:openai/openai-node:release",
        source_kind=SourceKind.GITHUB_RELEASE,
        source_url="https://example.test/release",
        old_token="3.3.0",
        new_token="4.0.0",
        body="Removed openai.createChatCompletion. Use openai.chat.completions.create instead.",
    )


def test_resolver_emits_only_a_feed_proven_candidate_for_an_observed_old_symbol() -> None:
    completer = _FakeCompleter(
        json.dumps(
            [
                {
                    "old_api": "openai.createChatCompletion",
                    "new_api": "openai.chat.completions.create",
                    "kind": "method_renamed",
                    "migration_guide": "Use the new client method.",
                    "evidence": "Removed openai.createChatCompletion. Use openai.chat.completions.create instead.",
                }
            ]
        )
    )

    result = UsageCandidateResolver(completer).resolve(
        _event(),
        package="openai",
        observed_symbols=["openai.createChatCompletion"],
    )

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.old_api == "openai.createChatCompletion"
    assert change.new_api == "openai.chat.completions.create"
    assert change.kind is ChangeKind.METHOD_RENAMED
    assert change.source is ClassificationSource.LLM_SYNTHESIS
    assert "openai.createChatCompletion" in completer.prompts[0]


def test_resolver_drops_a_candidate_for_a_symbol_not_observed_in_this_repo() -> None:
    completer = _FakeCompleter(
        json.dumps(
            [
                {
                    "old_api": "openai.createEmbedding",
                    "new_api": "openai.embeddings.create",
                    "kind": "method_renamed",
                    "evidence": "Removed openai.createChatCompletion. Use openai.chat.completions.create instead.",
                }
            ]
        )
    )

    result = UsageCandidateResolver(completer).resolve(
        _event(), package="openai", observed_symbols=["openai.createChatCompletion"]
    )

    assert result.changes == []


def test_resolver_accepts_a_feed_mapping_when_the_repo_already_uses_the_replacement() -> None:
    completer = _FakeCompleter(
        json.dumps(
            [
                {
                    "old_api": "openai.createChatCompletion",
                    "new_api": "openai.chat.completions.create",
                    "kind": "method_renamed",
                    "evidence": "Removed openai.createChatCompletion. Use openai.chat.completions.create instead.",
                }
            ]
        )
    )

    result = UsageCandidateResolver(completer).resolve(
        _event(), package="openai", observed_symbols=["openai.chat.completions.create"]
    )

    assert [(change.old_api, change.new_api) for change in result.changes] == [
        ("openai.createChatCompletion", "openai.chat.completions.create")
    ]
