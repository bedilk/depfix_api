"""Local-LLM code fix generator that talks to an Ollama server.

Mirrors the public surface of :class:`depfix.fixers.gemini.FixGenerator` so that
``DependencyFixAgent`` can swap providers without touching orchestration code.
Cost is always zero (local inference); tokens come from Ollama's
``prompt_eval_count`` / ``eval_count`` fields.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from depfix.core.models import BreakingChange, FileUsage, LLMCall, Usage
from depfix.redaction import RedactionError, redact_file_usage

logger = logging.getLogger(__name__)


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"


class OllamaFixGenerator:
    """Generate code fixes via a local Ollama server."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        max_retries: int = 3,
        temperature: float = 0.2,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.temperature = temperature
        self.timeout = timeout
        self.llm_calls: list[LLMCall] = []

    def generate_fix(
        self,
        file_usage: FileUsage,
        breaking_change: BreakingChange,
        *,
        feedback: str | None = None,
    ) -> tuple[str, float, LLMCall]:
        """Generate a fix for a file with breaking-change usages.

        ``feedback``, when set, is a description of why a previous attempt
        at fixing this file was rejected (syntax error or test regression),
        appended to the prompt so the model can course-correct on retry.
        """
        safe_usage, redaction = redact_file_usage(file_usage)
        safe_feedback = redaction.cover(feedback) if feedback else None
        if redaction.redacted:
            logger.info(
                "Redacted %d secret value(s) in %s before sending to %s",
                redaction.redacted_count,
                file_usage.filepath,
                self.model,
            )
        prompt = self._build_prompt(safe_usage, breaking_change, feedback=safe_feedback)

        for attempt in range(self.max_retries):
            try:
                start = time.time()
                payload = {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": self.temperature},
                }
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(f"{self.base_url}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()

                duration_ms = int((time.time() - start) * 1000)
                raw_text = data.get("response", "")

                input_tokens = int(data.get("prompt_eval_count", 0) or 0)
                output_tokens = int(data.get("eval_count", 0) or 0)

                llm_call = LLMCall(
                    timestamp=datetime.now(),
                    prompt=prompt,
                    response=raw_text,
                    model=self.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_estimate=0.0,  # local inference
                    duration_ms=duration_ms,
                )
                self.llm_calls.append(llm_call)

                fixed_code = redaction.restore(self._extract_code(raw_text))

                confidence = self._calculate_confidence(
                    file_usage.file_content,
                    fixed_code,
                    file_usage.usages,
                    breaking_change,
                )

                logger.info(
                    "Generated fix for %s (tokens: %d+%d, %dms, model=%s)",
                    file_usage.filepath,
                    input_tokens,
                    output_tokens,
                    duration_ms,
                    self.model,
                )
                return fixed_code, confidence, llm_call

            except RedactionError:
                raise
            except Exception as e:
                logger.warning("Ollama error on attempt %d: %s", attempt + 1, e)
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise

        # Unreachable — the loop either returns or raises.
        raise RuntimeError("unreachable")

    # -- prompt / parsing helpers (kept identical in spirit to gemini.py) ------

    def _build_prompt(
        self,
        file_usage: FileUsage,
        breaking_change: BreakingChange,
        *,
        feedback: str | None = None,
    ) -> str:
        usage_locations = "\n".join(
            f"  - Line {u.line_number}: {u.line_content.strip()}" for u in file_usage.usages
        )

        examples_section = ""
        if breaking_change.examples:
            examples = "\n\n".join(
                f"Before:\n```javascript\n{before}\n```\n\nAfter:\n```javascript\n{after}\n```"
                for before, after in breaking_change.examples
            )
            examples_section = f"\n\nEXAMPLES OF CORRECT MIGRATION:\n{examples}"

        feedback_section = ""
        if feedback:
            feedback_section = (
                f"\n\nPREVIOUS ATTEMPT FAILED:\n{feedback}\nFix the issue above in this attempt."
            )

        return f"""You are a senior developer performing a code migration for a breaking API change.

BREAKING CHANGE DETAILS:
- Package: {breaking_change.package}
- Version change: {breaking_change.old_version} → {breaking_change.new_version}
- Old API: {breaking_change.old_api}
- New API: {breaking_change.new_api}

MIGRATION GUIDE:
{breaking_change.migration_guide}
{examples_section}

TASK:
Update the following code to use the new API. The usages that need to be fixed are:
{usage_locations}

CURRENT CODE:
```javascript
{file_usage.file_content}
```

REQUIREMENTS:
1. Replace ALL usages of the old API with the new API
2. Preserve all other code exactly as-is
3. Maintain the same code style and formatting
4. Do not add any comments about the migration
5. Do not change any imports unless necessary for the new API
6. Tokens of the form DEPFIX_REDACTED_<hex> stand for redacted secrets: copy each one through unchanged, exactly where it appears
{feedback_section}
OUTPUT:
Return ONLY the complete updated code. No explanations, no markdown code blocks, just the raw code.
"""

    def _extract_code(self, response: str) -> str:
        code = response.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)
        return code

    def _calculate_confidence(
        self,
        original: str,
        fixed: str,
        usages: list[Usage],
        breaking_change: BreakingChange,
    ) -> float:
        confidence = 1.0
        if original.strip() == fixed.strip():
            return 0.0

        new_method = breaking_change.new_api.split("(")[0]
        if new_method.startswith("_."):
            new_method = new_method[2:]
        if new_method not in fixed:
            confidence -= 0.3

        old_method = breaking_change.old_api.split("(")[0]
        if old_method.startswith("_."):
            old_method = old_method[2:]

        old_count = fixed.lower().count(old_method.lower())
        orig_count = original.lower().count(old_method.lower())
        if old_count >= orig_count:
            confidence -= 0.4

        original_lines = set(original.split("\n"))
        fixed_lines = set(fixed.split("\n"))
        changed = len(original_lines.symmetric_difference(fixed_lines))
        if changed > len(usages) * 3:
            confidence -= 0.2

        return max(0.0, min(1.0, confidence))

    @property
    def total_cost(self) -> float:
        return 0.0  # local inference

    @property
    def total_tokens(self) -> int:
        return sum(call.total_tokens for call in self.llm_calls)
