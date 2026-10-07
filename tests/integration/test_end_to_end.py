"""End-to-end integration test — hits the real Gemini API.

Skipped unless GOOGLE_API_KEY is set. Marked `integration` so it is excluded
from the CI unit-test run (`pytest -m "not integration and not e2e"`).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from depfix.core.agent import DependencyFixAgent
from depfix.core.models import BreakingChange

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set — skipping live LLM integration test.",
)
def test_agent_fixes_openai_chat_completion_end_to_end(
    sample_codebase: Path, openai_chat_completion_change: BreakingChange
) -> None:
    agent = DependencyFixAgent(
        google_api_key=os.environ["GOOGLE_API_KEY"],
        model="gemini-2.5-flash",
    )
    result = agent.fix_breaking_change(
        breaking_change=openai_chat_completion_change,
        codebase_path=str(sample_codebase),
    )

    assert result.files_affected > 0
    assert result.success_rate > 0.0
    assert result.total_cost >= 0.0
    successful = result.successful_fixes
    assert successful, "Expected at least one successful fix"
    assert any("chat.completions.create" in f.fixed_code for f in successful)
