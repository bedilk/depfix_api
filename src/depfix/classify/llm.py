"""LLM seam for the release-notes classification path.

Deterministic spec-diff classification (``spec_rules.py``) never touches this
module — it exists only so ``notes.py`` can ask a model to read prose release
notes and propose structured changes, which are then gated by an
anti-hallucination verbatim-quote check before being trusted.

``temperature=0`` on every call: eval scores need to be stable run-to-run,
and classification is not a task where creative sampling helps.
"""

from __future__ import annotations

import logging
import os
import time
import typing
from dataclasses import dataclass
from typing import Protocol

import httpx

from depfix.fixers.gemini import GEMINI_PRICING
from depfix.obs.cost import CostLedger, CostStage

try:
    from google import genai
except Exception:  # pragma: no cover - optional dependency
    genai = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0
    duration_ms: int = 0


class LLMCompleter(Protocol):
    """Structural type for anything that can answer a classification prompt."""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse: ...


class GeminiCompleter:
    """Calls Gemini. Pricing table is shared with ``fixers.gemini`` — one
    source of truth for per-model rates rather than a second copy to drift."""

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.5-flash") -> None:
        if genai is None:
            raise RuntimeError("google-genai not installed. Install 'google-genai'.")
        if not api_key:
            api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Google API key not provided (GOOGLE_API_KEY or api_key param)")
        self._client = genai.Client(api_key=api_key)
        self.model = model

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        start = time.time()
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"temperature": temperature},
        )
        duration_ms = int((time.time() - start) * 1000)

        try:
            text = response.text
        except Exception:
            text = ""
            if getattr(response, "candidates", None):
                parts = getattr(response.candidates[0].content, "parts", None) or []  # type: ignore[index]
                text = "".join(getattr(p, "text", "") for p in parts)

        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
        pricing = GEMINI_PRICING.get(self.model, {"input": 1.5, "output": 10.0})
        cost = (input_tokens / 1_000_000) * pricing["input"] + (
            output_tokens / 1_000_000
        ) * pricing["output"]

        return LLMResponse(
            text=text,  # type: ignore[arg-type]
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
        )


class BedrockCompleter:
    """Calls Claude via AWS Bedrock using the anthropic SDK."""

    PRICING: typing.ClassVar[dict[str, dict[str, float]]] = {
        "us.anthropic.claude-opus-4-6-v1": {"input": 15.0, "output": 75.0},
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.0, "output": 15.0},
        "us.anthropic.claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0},
        "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 1.0, "output": 5.0},
        "us.anthropic.claude-haiku-3-5-20241022": {"input": 0.80, "output": 4.0},
        "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.0, "output": 15.0},
    }

    def __init__(
        self,
        *,
        aws_access_key: str = "",
        aws_secret_key: str = "",
        aws_region: str = "us-east-1",
        aws_profile: str = "",
        model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ) -> None:
        from anthropic import AnthropicBedrock

        kwargs: dict = {"aws_region": aws_region}
        if aws_access_key and aws_secret_key:
            kwargs["aws_access_key"] = aws_access_key
            kwargs["aws_secret_key"] = aws_secret_key
        elif aws_profile:
            kwargs["aws_profile"] = aws_profile
        self._client = AnthropicBedrock(**kwargs)
        self.model = model

    def complete(self, prompt: str, *, temperature: float = 0.1) -> LLMResponse:
        start = time.time()
        message = self._client.messages.create(
            model=self.model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"temperature": temperature},
        )
        duration_ms = int((time.time() - start) * 1000)

        text = str(getattr(message.content[0], "text", "")) if message.content else ""
        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens
        # Fall back to Sonnet-tier rates for unknown Bedrock model IDs rather
        # than Opus rates, which over-estimate cost by ~5x for most models.
        pricing = self.PRICING.get(self.model, {"input": 3.0, "output": 15.0})
        cost = (input_tokens / 1_000_000) * pricing["input"] + (
            output_tokens / 1_000_000
        ) * pricing["output"]

        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
        )


class OllamaCompleter:
    """Calls a local Ollama server. Cost is always zero (local inference)."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        start = time.time()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/api/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        duration_ms = int((time.time() - start) * 1000)
        return LLMResponse(
            text=data.get("response", ""),
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
            cost_estimate=0.0,
            duration_ms=duration_ms,
        )


class CostTrackingCompleter:
    """An ``LLMCompleter`` that records every call's cost against a ledger.

    Wraps any completer so the classify-time seams (notes, synthesis,
    usage-candidate discovery, the judge) are charged without each call site
    knowing the ledger exists.
    """

    def __init__(
        self, inner: LLMCompleter, ledger: CostLedger, stage: CostStage = CostStage.CLASSIFY
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._stage = stage

    def complete(self, prompt: str, *, temperature: float = 0.0) -> LLMResponse:
        response = self._inner.complete(prompt, temperature=temperature)
        self._ledger.record(
            self._stage,
            response.cost_estimate,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        return response

    def for_stage(self, stage: CostStage) -> CostTrackingCompleter:
        return CostTrackingCompleter(self._inner, self._ledger, stage)

    @property
    def unwrapped(self) -> LLMCompleter:
        return self._inner
