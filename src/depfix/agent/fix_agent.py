"""Bounded, test-grounded tool-assisted fix generation.

This strategy changes only how a candidate edit is produced.  The existing
pipeline still validates it, verifies it against the target repository, and
decides whether it can be committed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from depfix.agent.tools import FixToolset, ToolResult, tool_specs
from depfix.config import Settings
from depfix.core.models import BreakingChange, FileUsage, LLMCall
from depfix.fixers.gemini import FixGenerator
from depfix.fixers.ollama import OllamaFixGenerator
from depfix.redaction import redact_file_usage

logger = logging.getLogger(__name__)


@dataclass
class AgentTranscript:
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    steps_used: int = 0
    stopped_reason: str = ""


class FixAgent:
    """A drop-in fixer with at most ``max_steps`` checkout read operations."""

    def __init__(self, completer: FixGenerator | OllamaFixGenerator, *, max_steps: int = 6) -> None:
        self._completer = completer
        self._max_steps = max_steps
        self.llm_calls: list[LLMCall] = []
        self.last_transcript: AgentTranscript | None = None

    @property
    def total_cost(self) -> float:
        return sum(call.cost_estimate for call in self.llm_calls)

    @property
    def total_tokens(self) -> int:
        return sum(call.total_tokens for call in self.llm_calls)

    def generate_fix(
        self,
        file_usage: FileUsage,
        breaking_change: BreakingChange,
        *,
        feedback: str | None = None,
        toolset: FixToolset | None = None,
    ) -> tuple[str, float, LLMCall]:
        """Ask for bounded tool calls, then one complete replacement file."""
        if toolset is None:
            code, confidence, call = self._completer.generate_fix(
                file_usage, breaking_change, feedback=feedback
            )
            self.llm_calls.append(call)
            return code, confidence, call

        # Build prompts from the redacted copy; restore secrets in the output.
        safe_usage, redaction = redact_file_usage(file_usage)
        safe_feedback = redaction.cover(feedback) if feedback else None

        transcript = AgentTranscript()
        self.last_transcript = transcript
        context: list[str] = []
        for step in range(self._max_steps):
            transcript.steps_used = step + 1
            response, call = self._complete(
                self._prompt(safe_usage, breaking_change, safe_feedback, context, final=False)
            )
            parsed = self._json_response(response)
            tool = parsed.get("tool")
            if tool is None:
                code = str(parsed.get("code", ""))
                if code:
                    return self._proposal(
                        redaction.restore(code), file_usage, breaking_change, call
                    )
                context.append(
                    "The previous response was invalid; return the required JSON object."
                )
                continue
            result = self._dispatch(toolset, str(tool), parsed.get("arguments", {}))
            transcript.tool_calls.append(
                (str(tool), json.dumps(parsed.get("arguments", {}), sort_keys=True))
            )
            context.append(
                f"TOOL {tool} RESULT (ok={result.ok}):\n{redaction.cover(result.content)}"
            )
            if self._is_stalling(toolset):
                transcript.stopped_reason = "stall detected"
                break
        else:
            transcript.stopped_reason = "max_steps reached"

        response, call = self._complete(
            self._prompt(safe_usage, breaking_change, safe_feedback, context, final=True)
        )
        code = self._json_response(response).get("code")  # type: ignore[assignment]
        if not isinstance(code, str) or not code:
            raise RuntimeError("fix agent did not return a complete replacement file")
        return self._proposal(redaction.restore(code), file_usage, breaking_change, call)

    def _complete(self, prompt: str) -> tuple[str, LLMCall]:
        """Complete one constrained JSON turn and account for its cost."""
        started = time.time()
        if isinstance(self._completer, FixGenerator):
            model = self._completer.model
            response = self._completer.client.models.generate_content(
                model=model,
                contents=prompt,
                config={"temperature": self._completer.temperature},
            )
            try:
                raw = response.text
            except Exception:
                candidates = getattr(response, "candidates", None) or []
                parts = getattr(candidates[0].content, "parts", []) if candidates else []
                raw = "".join(getattr(part, "text", "") for part in parts)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
            cost = self._completer._calculate_cost(input_tokens, output_tokens)
        else:
            model = self._completer.model
            with httpx.Client(timeout=self._completer.timeout) as client:
                response = client.post(  # type: ignore[assignment]
                    f"{self._completer.base_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                        "options": {"temperature": self._completer.temperature},
                    },
                )
            response.raise_for_status()  # type: ignore[attr-defined]
            data = response.json()
            raw = str(data.get("response", ""))  # type: ignore[attr-defined]
            input_tokens = int(data.get("prompt_eval_count", 0) or 0)  # type: ignore[attr-defined]
            output_tokens = int(data.get("eval_count", 0) or 0)  # type: ignore[attr-defined]
            cost = 0.0
        call = LLMCall(
            timestamp=datetime.now(),
            prompt=prompt,
            response=raw,  # type: ignore[arg-type]
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=int((time.time() - started) * 1000),
        )
        self.llm_calls.append(call)
        return raw, call  # type: ignore[return-value]

    @staticmethod
    def _json_response(response: str) -> dict[str, Any]:
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _dispatch(toolset: FixToolset, name: str, arguments: object) -> ToolResult:
        args = arguments if isinstance(arguments, dict) else {}
        if name == "read_file":
            return toolset.read_file(str(args.get("relpath", "")))
        if name == "find_usages":
            return toolset.find_usages(str(args.get("symbol", "")))
        return ToolResult.error(f"unknown tool: {name}")

    @staticmethod
    def _is_stalling(toolset: FixToolset) -> bool:
        return len(toolset.call_log) != len(set(toolset.call_log))

    def _proposal(
        self, code: str, usage: FileUsage, change: BreakingChange, call: LLMCall
    ) -> tuple[str, float, LLMCall]:
        confidence = self._completer._calculate_confidence(
            usage.file_content, code, usage.usages, change
        )
        return code, confidence, call

    @staticmethod
    def _prompt(
        usage: FileUsage,
        change: BreakingChange,
        feedback: str | None,
        context: list[str],
        *,
        final: bool,
    ) -> str:
        locations = "\n".join(
            f"- line {item.line_number}: {item.line_content.strip()}" for item in usage.usages
        )
        mode = (
            'Return exactly {"tool": null, "code": "COMPLETE FILE"}.'
            if final
            else (
                "Return exactly one JSON object: either a tool request like "
                '{"tool":"read_file","arguments":{"relpath":"src/x.ts"}} or '
                '{"tool":"find_usages","arguments":{"symbol":"client.method"}}, '
                'or {"tool":null,"code":"COMPLETE FILE"}.'
            )
        )
        feedback_text = f"\nPrevious verification feedback:\n{feedback}" if feedback else ""
        context_text = "\n\n".join(context) or "(no extra repository context)"
        return f"""You are fixing one dependency migration.
Package: {change.package}; old API: {change.old_api}; new API: {change.new_api}
Migration guide: {change.migration_guide}
Target usages:\n{locations}
Current file ({usage.filepath}):\n{usage.file_content}
Read-only tools: {json.dumps(tool_specs())}
Collected context:\n{context_text}{feedback_text}
You may only request the listed tools. Preserve unrelated code.
Tokens of the form DEPFIX_REDACTED_<hex> stand for redacted secrets; copy each one through unchanged. {mode}"""


def make_fix_strategy(
    settings: Settings, fixer: FixGenerator | OllamaFixGenerator
) -> FixGenerator | OllamaFixGenerator | FixAgent:
    """Return the configured strategy; single-shot remains the default."""
    if settings.fix_strategy == "agent":
        return FixAgent(fixer, max_steps=settings.fix_agent_max_steps)
    return fixer
