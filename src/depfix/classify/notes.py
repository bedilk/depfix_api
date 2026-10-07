"""Release-notes classification: ask an LLM to read prose, trust nothing it
says unless it quotes the source verbatim.

Most providers don't publish an OpenAPI diff for every release — only prose
release notes. Precision here is lower by design: every proposed change must
carry an ``evidence`` quote that is checked, whitespace-insensitively,
against the actual notes text (``_quote_present``). A change whose quote
can't be found verbatim is dropped outright rather than kept at lower
confidence — a plausible-sounding hallucination is worse than a missed
change, because a missed change can still be caught some other way (spec
diff, corpus, manual triage) but a fabricated one erodes trust in every
other line of the report.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from depfix.classify.llm import LLMCompleter
from depfix.codemods.symbols import normalize_api
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource

logger = logging.getLogger(__name__)

# Fast pre-filter: registry events whose body contains none of these keywords
# are almost certainly additive releases and don't warrant an LLM call.
_BREAKING_MARKERS_RE = re.compile(
    r"\b(breaking|removed?|deprecated?|renamed?|migration|major)\b", re.IGNORECASE
)

_VALID_KINDS = {k.value for k in ChangeKind}

_PROMPT_TEMPLATE = """You are analyzing release notes for the package "{package}" \
upgrading from version {old_version} to {new_version}.

Identify BREAKING changes only — changes that require calling code to be \
updated. Ignore purely additive changes (new optional params, new endpoints, \
new fields).

Use method_deprecated when the notes say a method still works but is deprecated \
and will be removed in a future version. Prefer this over method_removed when \
the method is still callable today.

For each breaking change you MUST include a verbatim "evidence" quote — an \
exact substring copied from the release notes below, proving the change is \
real. Do not paraphrase the evidence. If you cannot find an exact quote for \
a change, do not report it.

Respond with a JSON array only, no prose, no markdown fences. Each element:
{{
  "kind": one of {kinds},
  "old_api": "the symbol/parameter/field that changed",
  "new_api": "the replacement, or empty string if there is none",
  "description": "one sentence describing the change",
  "evidence": "exact verbatim quote from the release notes"
}}

RELEASE NOTES:
---
{body}
---
"""


def _quote_present(evidence: str, body: str) -> bool:
    """Whitespace-insensitive substring check — the anti-hallucination gate."""
    norm_evidence = re.sub(r"\s+", " ", evidence).strip().lower()
    if not norm_evidence:
        return False
    norm_body = re.sub(r"\s+", " ", body).strip().lower()
    return norm_evidence in norm_body


def _parse_llm_json(text: str) -> list[Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


class NotesClassifier:
    """Classifies release-notes prose into ``BreakingChange`` rows via an LLM."""

    def __init__(self, completer: LLMCompleter, *, min_confidence: float = 0.5) -> None:
        self._completer = completer
        self._min_confidence = min_confidence
        self.last_cost: float = 0.0

    def classify(
        self,
        body: str,
        *,
        package: str,
        old_version: str,
        new_version: str,
        provider_id: str,
        source_url: str | None = None,
    ) -> list[BreakingChange]:
        if not body.strip():
            return []

        prompt = _PROMPT_TEMPLATE.format(
            package=package,
            old_version=old_version,
            new_version=new_version,
            kinds=sorted(_VALID_KINDS),
            body=body[:12000],
        )
        response = self._completer.complete(prompt, temperature=0.0)
        self.last_cost = response.cost_estimate
        candidates = _parse_llm_json(response.text)

        out: list[BreakingChange] = []
        for candidate in candidates:
            change = self._to_change(
                candidate,
                body=body,
                package=package,
                old_version=old_version,
                new_version=new_version,
                provider_id=provider_id,
                source_url=source_url,
            )
            if change is not None:
                out.append(change)
        return out

    def _to_change(
        self,
        candidate: Any,
        *,
        body: str,
        package: str,
        old_version: str,
        new_version: str,
        provider_id: str,
        source_url: str | None,
    ) -> BreakingChange | None:
        if not isinstance(candidate, dict):
            return None

        evidence = str(candidate.get("evidence") or "")
        if not _quote_present(evidence, body):
            logger.info(
                "dropping unverifiable LLM change for %s: evidence not found verbatim",
                package,
            )
            return None

        old_api = str(candidate.get("old_api") or "").strip()
        if not old_api:
            return None

        kind_raw = str(candidate.get("kind") or "unknown")
        kind = ChangeKind(kind_raw) if kind_raw in _VALID_KINDS else ChangeKind.UNKNOWN
        new_api = str(candidate.get("new_api") or "").strip()
        description = str(candidate.get("description") or "").strip() or old_api

        hints = [s for s in (normalize_api(old_api),) if s]

        try:
            change = BreakingChange(
                package=package,
                old_version=old_version,
                new_version=new_version,
                old_api=old_api,
                new_api=new_api,
                description=description,
                migration_guide="",
                kind=kind,
                source=ClassificationSource.RELEASE_NOTES,
                provider_id=provider_id,
                source_url=source_url,
                evidence=evidence,
                confidence=0.75,
                call_site_hints=hints,
            )
        except ValueError:
            return None

        if change.confidence < self._min_confidence:
            return None
        return change
