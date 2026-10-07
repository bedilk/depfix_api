"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def sample_codebase(fixtures_dir: Path) -> Path:
    """A small JS codebase written against openai-node v3."""
    return fixtures_dir / "openai_v3_project"


@pytest.fixture
def openai_chat_completion_change() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.3.0",
        new_version="4.0.0",
        old_api="openai.createChatCompletion(params)",
        new_api="openai.chat.completions.create(params)",
        description=(
            "`openai.createChatCompletion()` was replaced by "
            "`openai.chat.completions.create()`; the response is returned "
            "directly, no longer wrapped in `.data`."
        ),
        migration_guide=(
            "Replace `openai.createChatCompletion(params)` with "
            "`openai.chat.completions.create(params)`, and drop the leading "
            "`.data` when reading the response (`response.choices` instead "
            "of `response.data.choices`)."
        ),
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
        confidence=0.9,
        examples=[
            (
                "const response = await openai.createChatCompletion({ model, messages });\n"
                "return response.data.choices[0].message.content;",
                "const response = await openai.chat.completions.create({ model, messages });\n"
                "return response.choices[0].message.content;",
            )
        ],
    )
