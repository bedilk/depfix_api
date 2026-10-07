"""Grounded LLM synthesis of a mechanically actionable migration spec."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from depfix.classify.llm import LLMCompleter
from depfix.classify.notes import _quote_present
from depfix.codemods.symbols import normalize_api
from depfix.core.models import REMOVAL_KINDS, BreakingChange, ChangeKind, ClassificationSource
from depfix.sources.models import ChangeEvent, SpecChange, SpecChangeKind


@dataclass(frozen=True)
class SynthesisResult:
    change: BreakingChange | None = None
    reason: str = ""
    cost_usd: float = 0.0


def _parse_response(text: str) -> dict[str, object] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _kind_for(new_api: str | None, changes: list[SpecChange]) -> ChangeKind:
    if new_api is not None:
        return ChangeKind.METHOD_RENAMED
    mapping = {
        SpecChangeKind.PATH_REMOVED: ChangeKind.METHOD_REMOVED,
        SpecChangeKind.OPERATION_REMOVED: ChangeKind.METHOD_REMOVED,
        SpecChangeKind.PROPERTY_REMOVED: ChangeKind.FIELD_REMOVED,
        SpecChangeKind.PARAM_REMOVED: ChangeKind.PARAM_REMOVED,
    }
    return next(
        (mapping[change.kind] for change in changes if change.kind in mapping), ChangeKind.UNKNOWN
    )


class MigrationSynthesizer:
    """Return a migration only when source evidence and symbols both validate."""

    def __init__(self, completer: LLMCompleter) -> None:
        self._completer = completer

    def synthesize(self, event: ChangeEvent, *, package: str) -> SynthesisResult:
        source_text = "\n\n".join(
            part
            for part in (event.body, *(change.one_line() for change in event.spec_changes))
            if part
        )
        if not source_text.strip():
            return SynthesisResult(reason="no source text")
        response = self._completer.complete(
            "Return JSON with old_api, new_api, migration_guide, call_site_hints, evidence. "
            "Both APIs must be dotted symbols or empty. Evidence must be copied verbatim.\n\n"
            + source_text[:12000],
            temperature=0.0,
        )
        payload = _parse_response(response.text)
        if payload is None:
            return SynthesisResult(
                reason="LLM response was not JSON", cost_usd=response.cost_estimate
            )
        evidence = str(payload.get("evidence") or "")
        if not _quote_present(evidence, source_text):
            return SynthesisResult(
                reason="evidence is not verbatim", cost_usd=response.cost_estimate
            )
        old_api = normalize_api(str(payload.get("old_api") or ""))
        new_api = normalize_api(str(payload.get("new_api") or ""))
        if old_api is None:
            # Do not create a synthetic record with an invented/no scanner
            # target. The deterministic structural classifier remains the
            # conservative report-only fallback.
            return SynthesisResult(reason="no actionable old_api", cost_usd=response.cost_estimate)
        kind = _kind_for(new_api, event.spec_changes)
        if new_api is None and kind not in REMOVAL_KINDS:
            return SynthesisResult(
                reason="no replacement for non-removal", cost_usd=response.cost_estimate
            )
        hints = [str(h).strip() for h in payload.get("call_site_hints", []) if isinstance(h, str)][  # type: ignore[attr-defined]
            :5
        ]
        change = BreakingChange(
            package=package,
            old_version=event.old_token or "",
            new_version=event.new_token,
            old_api=old_api,
            new_api=new_api or "",
            description=str(payload.get("migration_guide") or old_api),
            migration_guide=str(payload.get("migration_guide") or ""),
            kind=kind,
            source=ClassificationSource.LLM_SYNTHESIS,
            provider_id=event.provider_id,
            source_url=event.source_url,
            evidence=evidence,
            confidence=0.75,
            call_site_hints=hints,
        )
        return SynthesisResult(change=change, cost_usd=response.cost_estimate)
