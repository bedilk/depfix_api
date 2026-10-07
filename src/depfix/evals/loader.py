"""Loads and validates the YAML eval corpus.

``verified`` is mandatory on every case — the loader raises rather than
defaulting it, because a silently-defaulted ``verified: true`` is exactly
the kind of thing that inflates a pass rate without anyone noticing. An
unverified case is still runnable (useful while drafting a new one) but
``depfix eval`` reports it separately from the verified pass rate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from depfix.evals.models import (
    EvalCase,
    EvalCategory,
    ExpectedCallSite,
    ExpectedChange,
    FixAssertion,
)


class CorpusError(ValueError):
    """Raised for any structurally invalid corpus entry."""


def _require(data: dict[str, Any], key: str, case_id: str) -> Any:
    if key not in data:
        raise CorpusError(f"case '{case_id}' is missing required field '{key}'")
    return data[key]


def _parse_expected_changes(data: dict[str, Any], case_id: str) -> list[ExpectedChange]:
    out = []
    for item in data.get("expected_changes", []):
        if "kind" not in item:
            raise CorpusError(f"case '{case_id}': expected_changes entry missing 'kind'")
        raw_also = item.get("also_accept", [])
        if not isinstance(raw_also, list) or not all(isinstance(k, str) for k in raw_also):
            raise CorpusError(f"case '{case_id}': 'also_accept' must be a list of strings")
        out.append(
            ExpectedChange(
                kind=item["kind"],
                old_api_contains=item.get("old_api_contains", ""),
                min_confidence=float(item.get("min_confidence", 0.0)),
                also_accept=tuple(raw_also),
            )
        )
    return out


def _parse_expected_call_sites(data: dict[str, Any], case_id: str) -> list[ExpectedCallSite]:
    out = []
    for item in data.get("expected_call_sites", []):
        if "symbol_contains" not in item and "kind" not in item:
            raise CorpusError(
                f"case '{case_id}': expected_call_sites entry needs 'symbol_contains' or 'kind'"
            )
        out.append(
            ExpectedCallSite(
                symbol_contains=str(item.get("symbol_contains", "")),
                kind=str(item.get("kind", "")),
                file_contains=str(item.get("file_contains", "")),
                min_confidence=str(item.get("min_confidence", "low")),
            )
        )
    return out


def _parse_fix_assertion(data: dict[str, Any]) -> FixAssertion | None:
    if "fix_assertion" not in data:
        return None
    fa = data["fix_assertion"] or {}
    return FixAssertion(
        must_contain=list(fa.get("must_contain", [])),
        must_not_contain=list(fa.get("must_not_contain", [])),
        max_changed_line_ratio=fa.get("max_changed_line_ratio"),
    )


def _parse_case(data: dict[str, Any]) -> EvalCase:
    case_id = str(data.get("id") or "<unknown>")
    verified = _require(data, "verified", case_id)
    if not isinstance(verified, bool):
        raise CorpusError(f"case '{case_id}': 'verified' must be true/false, got {verified!r}")

    category_raw = _require(data, "category", case_id)
    try:
        category = EvalCategory(category_raw)
    except ValueError as exc:
        valid = ", ".join(c.value for c in EvalCategory)
        raise CorpusError(
            f"case '{case_id}': unknown category '{category_raw}' (valid: {valid})"
        ) from exc

    # spec_diff and call_sites are both deterministic (no LLM in the loop);
    # release_notes and fix_generation both need one.
    default_requires_llm = category not in (EvalCategory.SPEC_DIFF, EvalCategory.CALL_SITES)

    min_recall = data.get("min_recall")

    return EvalCase(
        id=case_id,
        category=category,
        verified=verified,
        description=str(data.get("description", "")),
        requires_llm=bool(data.get("requires_llm", default_requires_llm)),
        raw=data,
        package=str(data.get("package", "")),
        old_version=str(data.get("old_version", "")),
        new_version=str(data.get("new_version", "")),
        old_spec=data.get("old_spec"),
        new_spec=data.get("new_spec"),
        expected_changes=_parse_expected_changes(data, case_id),
        release_notes_body=str(data.get("release_notes_body", "")),
        breaking_change=data.get("breaking_change"),
        fixture=str(data.get("fixture", "")),
        target_file=str(data.get("target_file", "")),
        fix_assertion=_parse_fix_assertion(data),
        provider_id=str(data.get("provider_id", "")),
        sdk_packages=list(data.get("sdk_packages", [])),
        api_base_urls=list(data.get("api_base_urls", [])),
        symbols=list(data.get("symbols", [])),
        expected_call_sites=_parse_expected_call_sites(data, case_id),
        forbidden_files=list(data.get("forbidden_files", [])),
        min_recall=float(min_recall) if min_recall is not None else None,
    )


def load_corpus_file(path: Path) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise CorpusError(f"{path}: corpus file must be a YAML list of cases")

    cases = [_parse_case(item) for item in raw]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise CorpusError(f"{path}: duplicate case id '{case.id}'")
        seen.add(case.id)
    return cases


def load_corpus_dir(directory: Path) -> list[EvalCase]:
    """Load every ``*.yaml`` file in ``directory``, sorted for deterministic
    ordering (report diffs shouldn't shuffle just because the filesystem did)."""
    directory = Path(directory)
    if not directory.exists():
        raise CorpusError(f"corpus directory does not exist: {directory}")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        for case in load_corpus_file(path):
            if case.id in seen:
                raise CorpusError(f"duplicate case id '{case.id}' across corpus files")
            seen.add(case.id)
            cases.append(case)
    return cases
