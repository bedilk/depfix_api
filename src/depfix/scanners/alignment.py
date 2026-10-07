"""Symbol alignment judge — one cheap LLM call per (provider, commit).

When a change's ``old_api`` matches nothing in the scan, the current
pipeline returns ``NO_CALL_SITES``. This module asks one batched question
with no file contents — just two short lists of symbols — to map
documented API changes onto symbols the repository actually calls.

Properties that keep it cheap and safe:

* One call per ``(provider, commit)``, not per change.
* ~1-2K tokens -- no source code, just symbol lists. Around $0.002.
* Verifiable: any ``observed`` value not in the input list is dropped.
* Cacheable on ``sha256(provider | sorted(observed) | sorted(change_keys))``.
* ``certain: false`` caps the site at MEDIUM confidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from depfix.core.models import BreakingChange

logger = logging.getLogger(__name__)

_ALIGN_PROMPT = """You are mapping documented API changes onto symbols a repository
actually calls. You never invent symbols.

SDK surface ({package}@{version}), relevant subset:
{surface}

Symbols observed in this repository:
{observed}

Documented changes (old API as written upstream, plus its verbatim evidence):
{changes}

For each change, return the OBSERVED symbols that are the same API. Return JSON:
[{{"old_api": "...", "observed": ["..."], "why": "...", "certain": true|false}}]

Rules:
- `observed` entries must be copied verbatim from the observed list.
- Return an empty list when the repository does not call that API.
- `certain` is false when you are inferring from naming alone.
"""


@dataclass(frozen=True)
class AlignmentResult:
    """One resolved mapping from a documented change to observed symbols."""

    old_api: str
    observed: tuple[str, ...]
    why: str
    certain: bool


def _cache_key(provider_id: str, observed: list[str], change_keys: list[str]) -> str:
    blob = json.dumps(
        {
            "provider": provider_id,
            "observed": sorted(observed),
            "changes": sorted(change_keys),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / "alignments" / f"{key}.json"


def _load_cached(path: Path) -> list[AlignmentResult] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            AlignmentResult(
                old_api=entry["old_api"],
                observed=tuple(entry["observed"]),
                why=entry["why"],
                certain=entry["certain"],
            )
            for entry in data
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _save_cached(path: Path, results: list[AlignmentResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "old_api": r.old_api,
                    "observed": list(r.observed),
                    "why": r.why,
                    "certain": r.certain,
                }
                for r in results
            ]
        ),
        encoding="utf-8",
    )


def build_alignment_prompt(
    *,
    package: str,
    version: str,
    surface_symbols: list[str],
    observed_symbols: list[str],
    changes: list[BreakingChange],
) -> str:
    """Build the alignment prompt from two symbol lists and a change list."""
    surface = (
        "\n".join(f"  {s}" for s in sorted(surface_symbols)[:200]) or "  (no surface available)"
    )
    observed = "\n".join(f"  {s}" for s in sorted(observed_symbols)[:200]) or "  (none)"
    change_lines = "\n".join(
        f"  - old_api: {c.old_api!r}, evidence: {(c.evidence or '')[:200]!r}" for c in changes[:30]
    )
    return _ALIGN_PROMPT.format(
        package=package,
        version=version,
        surface=surface,
        observed=observed,
        changes=change_lines,
    )


def parse_alignment_response(
    raw: str,
    *,
    valid_observed: frozenset[str],
) -> list[AlignmentResult]:
    """Parse the LLM response and drop any hallucinated symbols."""
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start < 0 or end <= start:
            return []
        data = json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        logger.debug("alignment: failed to parse LLM response")
        return []

    results: list[AlignmentResult] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        old_api = str(entry.get("old_api", ""))
        raw_observed = entry.get("observed", [])
        if not isinstance(raw_observed, list):
            continue
        filtered = tuple(s for s in raw_observed if isinstance(s, str) and s in valid_observed)
        results.append(
            AlignmentResult(
                old_api=old_api,
                observed=filtered,
                why=str(entry.get("why", "")),
                certain=bool(entry.get("certain", False)),
            )
        )
    return results


def align_symbols(
    *,
    completer: object,
    package: str,
    version: str,
    surface_symbols: list[str],
    observed_symbols: list[str],
    changes: list[BreakingChange],
    cache_dir: Path | None = None,
    provider_id: str = "",
    ledger: object | None = None,
) -> list[AlignmentResult]:
    """Run the alignment judge: one LLM call mapping changes to observed symbols.

    Returns cached results when available. The ``completer`` must implement
    a ``complete(prompt, **kwargs)`` interface compatible with depfix's LLM
    abstraction.
    """
    if not changes or not observed_symbols:
        return []

    change_keys = [c.dedupe_key for c in changes]
    key = _cache_key(provider_id or package, observed_symbols, change_keys)

    if cache_dir is not None:
        cached = _load_cached(_cache_path(cache_dir, key))
        if cached is not None:
            logger.debug("alignment: cache hit for %s (%d results)", provider_id, len(cached))
            return cached

    prompt = build_alignment_prompt(
        package=package,
        version=version,
        surface_symbols=surface_symbols,
        observed_symbols=observed_symbols,
        changes=changes,
    )

    try:
        response = completer.complete(prompt, max_tokens=2000)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("alignment: LLM call failed for %s: %s", provider_id, exc)
        return []

    if ledger is not None:
        import contextlib

        from depfix.obs.cost import CostStage

        with contextlib.suppress(Exception):
            ledger.record(CostStage.SYMBOL_ALIGNMENT, getattr(response, "cost_usd", 0.0))  # type: ignore[attr-defined]

    raw_text = getattr(response, "text", str(response))
    valid_observed = frozenset(observed_symbols)
    results = parse_alignment_response(raw_text, valid_observed=valid_observed)

    if cache_dir is not None:
        _save_cached(_cache_path(cache_dir, key), results)

    logger.info(
        "alignment: %s → %d mappings (%d with matches)",
        provider_id,
        len(results),
        sum(1 for r in results if r.observed),
    )
    return results
