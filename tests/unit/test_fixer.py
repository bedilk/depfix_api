"""Unit tests for the Gemini-based fix generator (fully mocked — no API calls)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from depfix.core.models import BreakingChange, FileUsage, Usage
from depfix.fixers.gemini import GEMINI_PRICING, FixGenerator


@pytest.fixture
def file_usage() -> FileUsage:
    content = "const response = await openai.createChatCompletion({ model, messages });\n"
    return FileUsage(
        filepath="src/chat.js",
        file_content=content,
        usages=[
            Usage(
                line_number=1,
                column=25,
                line_content=content.rstrip("\n"),
                match_text="openai.createChatCompletion(",
            )
        ],
    )


def _fake_response(text: str, in_tok: int = 100, out_tok: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=in_tok,
            candidates_token_count=out_tok,
        ),
    )


def test_prompt_includes_breaking_change_metadata(
    file_usage: FileUsage, openai_chat_completion_change: BreakingChange
) -> None:
    with patch("depfix.fixers.gemini.genai") as mock_genai:
        mock_genai.Client.return_value.models.generate_content.return_value = _fake_response(
            "const response = await openai.chat.completions.create({ model, messages });\n"
        )

        gen = FixGenerator(api_key="fake", model="gemini-2.5-flash")
        prompt = gen._build_prompt(file_usage, openai_chat_completion_change)

    assert "openai" in prompt
    assert "createChatCompletion" in prompt
    assert "chat.completions.create" in prompt
    assert "3.3.0" in prompt and "4.0.0" in prompt
    assert "Line 1" in prompt


def test_extract_code_strips_markdown_fence() -> None:
    with patch("depfix.fixers.gemini.genai"):
        gen = FixGenerator(api_key="fake")
    fenced = "```javascript\nconst x = 1;\n```"
    assert gen._extract_code(fenced) == "const x = 1;"


def test_generate_fix_records_llm_call(
    file_usage: FileUsage, openai_chat_completion_change: BreakingChange
) -> None:
    fixed_src = "const response = await openai.chat.completions.create({ model, messages });\n"

    with patch("depfix.fixers.gemini.genai") as mock_genai:
        client = MagicMock()
        client.models.generate_content.return_value = _fake_response(fixed_src, 120, 40)
        mock_genai.Client.return_value = client

        gen = FixGenerator(api_key="fake", model="gemini-2.5-flash", max_retries=1)
        fixed_code, confidence, call = gen.generate_fix(file_usage, openai_chat_completion_change)

    assert fixed_code == fixed_src.strip()
    assert 0.0 <= confidence <= 1.0
    assert call.input_tokens == 120
    assert call.output_tokens == 40
    assert call.model == "gemini-2.5-flash"
    assert len(gen.llm_calls) == 1


def test_cost_calculation_uses_pricing_table() -> None:
    with patch("depfix.fixers.gemini.genai"):
        gen = FixGenerator(api_key="fake", model="gemini-2.5-flash")
    cost = gen._calculate_cost(1_000_000, 1_000_000)
    expected = (
        GEMINI_PRICING["gemini-2.5-flash"]["input"] + GEMINI_PRICING["gemini-2.5-flash"]["output"]
    )
    assert cost == pytest.approx(expected)


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with patch("depfix.fixers.gemini.genai"):
        with pytest.raises(RuntimeError, match="Google API key"):
            FixGenerator(api_key=None)
