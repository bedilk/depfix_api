"""Derive feed-grounded migration candidates for APIs a repository uses.

The resolver accepts one feed event and the scanner's observed symbols, and
returns only candidates whose old API is both documented by that event and
present in that repository. It does not manufacture migrations from a static
catalog.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from depfix.classify.llm import LLMCompleter
from depfix.classify.models import ClassifyOutcome
from depfix.classify.notes import _quote_present
from depfix.classify.store import event_from_row, persist_classification
from depfix.codemods.symbols import normalize_api
from depfix.core.models import REMOVAL_KINDS, BreakingChange, ChangeKind, ClassificationSource
from depfix.sources.models import ChangeEvent
from depfix.storage.schema import BreakingChangeRow, ChangeEventRow

logger = logging.getLogger(__name__)


def _leaf(symbol: str) -> str:
    """Last dotted segment: ``openai.chat.completions.create`` → ``create``."""
    return symbol.rsplit(".", 1)[-1] if "." in symbol else symbol


def _resource(symbol: str) -> str:
    """Second-to-last segment: ``openai.chat.completions.create`` → ``completions``.

    Requires at least 3 parts to be useful. A 2-part symbol like
    ``sentry.withScope`` has the provider name as its only second segment
    (``sentry``), which is shared by every symbol in that provider's namespace
    and would cause false-positive matches. Only 3+ part dotted paths carry a
    meaningful resource namespace.
    """
    parts = symbol.split(".")
    return parts[-2] if len(parts) >= 3 else ""


def _plausibly_related(observed: str, candidate: str) -> bool:
    """Shared leaf OR shared resource prefix.

    Single-character or short segments (``get``, ``set``, ``on``) are
    false-positive magnets, so require length >= 4 on the shared segment —
    same bar ``_qualify_symbol`` already uses.
    """
    o_leaf, c_leaf = _leaf(observed).lower(), _leaf(candidate).lower()
    if o_leaf == c_leaf and len(o_leaf) >= 4:
        return True
    o_res, c_res = _resource(observed).lower(), _resource(candidate).lower()
    return bool(o_res) and o_res == c_res and len(o_res) >= 4


def _event_touches_observed(event: ChangeEvent, observed: list[str]) -> bool:
    """Does any symbol named in this event plausibly relate to any observed
    old symbol? A negative is cheap; a positive earns the full LLM prompt.

    Checks both structural spec_changes (strong signal) and prose body (leaf
    substring search). The prose search is a pre-filter only — it can false-
    positive, but the LLM prompt's _candidate_from_item evidence guard catches
    those. What we're preventing is the sentry-style complete miss: 477 v11
    events x 6 v6 symbols that share nothing.
    """
    corpus_symbols: set[str] = set()
    for change in event.spec_changes:
        if change.subject:
            corpus_symbols.add(change.subject)
        if change.before:
            corpus_symbols.add(change.before)
        if change.after:
            corpus_symbols.add(change.after)

    # Structural match: any observed symbol plausibly related to any spec symbol.
    if corpus_symbols and any(
        _plausibly_related(obs, cand) for obs in observed for cand in corpus_symbols
    ):
        return True

    # Prose match: leaf of any observed symbol appears in the event body.
    body = event.body or ""
    for obs in observed:
        leaf = _leaf(obs)
        if len(leaf) >= 4 and leaf in body:
            return True

    return False


@dataclass(frozen=True)
class UsageCandidateResult:
    """Candidates accepted from one feed event, plus the reason when empty."""

    changes: list[BreakingChange]
    reason: str = ""


class UsageCandidateResolver:
    """Find documented migrations relevant to one repository's observed calls."""

    def __init__(self, completer: LLMCompleter, *, min_confidence: float = 0.75) -> None:
        self._completer = completer
        self._min_confidence = min_confidence

    def resolve(
        self,
        event: ChangeEvent,
        *,
        package: str,
        observed_symbols: list[str],
    ) -> UsageCandidateResult:
        observed = sorted({symbol for symbol in observed_symbols if normalize_api(symbol)})
        source_text = _source_text(event)
        if not observed:
            return UsageCandidateResult([], "no resolved repository API symbols")
        if not source_text:
            return UsageCandidateResult([], "feed provides no release-note or spec evidence")

        response = self._completer.complete(
            _prompt(event, package=package, observed_symbols=observed, source_text=source_text),
            temperature=0.0,
        )
        payload = _parse_array(response.text)
        changes: list[BreakingChange] = []
        for item in payload:
            candidate = _candidate_from_item(
                item,
                event=event,
                package=package,
                source_text=source_text,
                observed=set(observed),
                confidence=self._min_confidence,
            )
            if candidate is not None:
                changes.append(candidate)
        return UsageCandidateResult(changes, "no feed-proven migration matches observed APIs")


def discover_usage_candidates(
    session: Session,
    *,
    provider_id: str,
    package: str,
    observed_symbols: list[str],
    completer: LLMCompleter,
    limit: int = 10,
) -> list[BreakingChange]:
    """Persist feed-proven candidates relevant to observed repository APIs.

    This is the scan/classification seam. It deliberately considers only the
    newest evidence-bearing feed events and never turns an unsupported version
    movement into an API migration. Existing records for an old API are reused
    instead of repeatedly asking an LLM about the same documented change.
    """
    observed = sorted({symbol for symbol in observed_symbols if normalize_api(symbol)})
    if not observed:
        return []
    existing = set(
        session.scalars(
            select(BreakingChangeRow.old_api).where(
                BreakingChangeRow.provider_id == provider_id,
                BreakingChangeRow.old_api.in_(observed),
            )
        )
    )
    events = session.scalars(
        select(ChangeEventRow)
        .where(
            ChangeEventRow.provider_id == provider_id,
            ChangeEventRow.body.is_not(None),
            ChangeEventRow.body != "",
        )
        .order_by(ChangeEventRow.detected_at.desc())
        .limit(limit)
    ).all()
    resolver = UsageCandidateResolver(completer)
    discovered: list[BreakingChange] = []
    skipped = 0
    for row in events:
        event = event_from_row(row)
        if not _event_touches_observed(event, observed):
            logger.debug(
                "skipping event %s for %s: no shared leaf/resource with observed symbols",
                row.id,
                provider_id,
            )
            skipped += 1
            continue
        result = resolver.resolve(event, package=package, observed_symbols=observed)
        changes = [change for change in result.changes if change.old_api not in existing]
        if not changes:
            continue
        persist_classification(
            session,
            row,
            ClassifyOutcome(changes=changes),
        )
        existing.update(change.old_api for change in changes)
        discovered.extend(changes)
    if skipped:
        logger.debug(
            "pre-filter skipped %d/%d event(s) for %s (no leaf/resource overlap)",
            skipped,
            skipped + len(discovered),
            provider_id,
        )
    return discovered


def _source_text(event: ChangeEvent) -> str:
    return "\n\n".join(
        value
        for value in (
            event.body,
            *(change.one_line() for change in event.spec_changes),
        )
        if value
    )


def _prompt(
    event: ChangeEvent,
    *,
    package: str,
    observed_symbols: list[str],
    source_text: str,
) -> str:
    return (
        "Extract only provider-documented breaking migrations relevant to this "
        "repository. Return a JSON array; each object has old_api, new_api, kind, "
        "migration_guide, evidence. Either old_api or new_api MUST exactly equal one "
        "OBSERVED SYMBOL. new_api must be a dotted replacement or empty only for a "
        "removal. evidence "
        "must be an exact quote from FEED EVIDENCE. Return [] when the feed cannot "
        "prove a migration for an observed symbol.\n\n"
        f"PACKAGE: {package}\nVERSION: {event.old_token or '?'} -> {event.new_token}\n"
        f"OBSERVED SYMBOLS: {json.dumps(observed_symbols)}\n\n"
        f"FEED EVIDENCE:\n{source_text[:12000]}"
    )


def _parse_array(text: str) -> list[object]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match is None:
            return []
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return []
    return parsed if isinstance(parsed, list) else []


def _candidate_from_item(
    item: object,
    *,
    event: ChangeEvent,
    package: str,
    source_text: str,
    observed: set[str],
    confidence: float,
) -> BreakingChange | None:
    if not isinstance(item, dict):
        return None
    old_api = normalize_api(str(item.get("old_api") or ""))
    new_api = normalize_api(str(item.get("new_api") or ""))
    evidence = str(item.get("evidence") or "")
    kind_raw = str(item.get("kind") or ChangeKind.UNKNOWN.value)
    kind = ChangeKind(kind_raw) if kind_raw in ChangeKind._value2member_map_ else ChangeKind.UNKNOWN
    if old_api is None or not _quote_present(evidence, source_text):
        return None
    if new_api is None and kind not in REMOVAL_KINDS:
        return None
    if old_api not in observed and (new_api is None or new_api not in observed):
        return None
    try:
        return BreakingChange(
            package=package,
            old_version=event.old_token or "",
            new_version=event.new_token,
            old_api=old_api,
            new_api=new_api or "",
            description=str(item.get("migration_guide") or old_api),
            migration_guide=str(item.get("migration_guide") or ""),
            kind=kind,
            source=ClassificationSource.LLM_SYNTHESIS,
            provider_id=event.provider_id,
            source_url=event.source_url,
            evidence=evidence,
            confidence=confidence,
            call_site_hints=[old_api],
        )
    except ValueError:
        return None
