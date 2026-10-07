"""Bedrock-hosted Claude fix generator.

Uses the ``anthropic`` SDK's ``AnthropicBedrock`` client to call Claude
models via AWS Bedrock. Mirrors the public surface of
:class:`depfix.fixers.gemini.FixGenerator` so the orchestration layer can
swap providers without touching its own code.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from depfix.core.models import BreakingChange, FileUsage, LLMCall, Usage
from depfix.redaction import RedactionError, redact_file_usage

logger = logging.getLogger(__name__)

BEDROCK_PRICING = {
    "us.anthropic.claude-opus-4-6-v1": {"input": 15.0, "output": 75.0},
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 1.0, "output": 5.0},
    "us.anthropic.claude-haiku-3-5-20241022": {"input": 0.80, "output": 4.0},
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.0, "output": 15.0},
}


class BedrockFixGenerator:
    """Generate code fixes via Claude on AWS Bedrock."""

    def __init__(
        self,
        *,
        aws_access_key: str = "",
        aws_secret_key: str = "",
        aws_region: str = "us-east-1",
        aws_profile: str = "",
        model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        max_retries: int = 3,
        temperature: float = 0.1,
    ) -> None:
        from anthropic import AnthropicBedrock

        kwargs: dict = {"aws_region": aws_region}
        if aws_access_key and aws_secret_key:
            kwargs["aws_access_key"] = aws_access_key
            kwargs["aws_secret_key"] = aws_secret_key
        elif aws_profile:
            kwargs["aws_profile"] = aws_profile
        self.client = AnthropicBedrock(**kwargs)
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature
        self.llm_calls: list[LLMCall] = []

    def generate_fix(
        self,
        file_usage: FileUsage,
        breaking_change: BreakingChange,
        *,
        feedback: str | None = None,
    ) -> tuple[str, float, LLMCall]:
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
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=16384,
                    messages=[{"role": "user", "content": prompt}],
                    extra_body={"temperature": self.temperature},
                )
                duration_ms = int((time.time() - start) * 1000)

                raw_text = str(getattr(message.content[0], "text", "")) if message.content else ""
                fixed_code = redaction.restore(self._extract_code(raw_text))

                input_tokens = message.usage.input_tokens
                output_tokens = message.usage.output_tokens
                cost = self._calculate_cost(input_tokens, output_tokens)

                llm_call = LLMCall(
                    timestamp=datetime.now(),
                    prompt=prompt,
                    response=raw_text,
                    model=self.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_estimate=cost,
                    duration_ms=duration_ms,
                )
                self.llm_calls.append(llm_call)

                confidence = self._calculate_confidence(
                    file_usage.file_content,
                    fixed_code,
                    file_usage.usages,
                    breaking_change,
                )

                logger.info(
                    "Generated fix for %s (tokens: %d+%d, cost: $%.4f, model=%s)",
                    file_usage.filepath,
                    input_tokens,
                    output_tokens,
                    cost,
                    self.model,
                )
                return fixed_code, confidence, llm_call

            except RedactionError:
                raise
            except Exception as e:
                logger.warning("Bedrock error on attempt %d: %s", attempt + 1, e)
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise

        raise RuntimeError("generate_fix: exhausted retries without returning or raising")

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
6. Tokens of the form DEPFIX_REDACTED_<hex> stand for redacted secrets: copy each one
   through unchanged, exactly where it appears
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

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        # Fall back to Sonnet-tier rates for unknown model IDs rather than
        # Opus rates, which over-estimate cost by ~5x for most models.
        pricing = BEDROCK_PRICING.get(self.model, {"input": 3.0, "output": 15.0})
        return (input_tokens / 1_000_000) * pricing["input"] + (
            output_tokens / 1_000_000
        ) * pricing["output"]

    @property
    def total_cost(self) -> float:
        return sum(call.cost_estimate for call in self.llm_calls)

    @property
    def total_tokens(self) -> int:
        return sum(call.total_tokens for call in self.llm_calls)
