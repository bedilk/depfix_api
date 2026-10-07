"""Optional, bounded LLM refinement for scanner-discovered call sites."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from depfix.classify.llm import LLMCompleter
from depfix.core.models import BreakingChange
from depfix.redaction import redact_text
from depfix.scanners.models import CallSite, MatchConfidence


@dataclass(frozen=True)
class JudgeVerdict:
    filepath: str
    line_number: int
    affected: bool
    why: str


class LLMCallSiteJudge:
    """Refine only already-discovered sites; never invents paths or sites."""

    def __init__(self, completer: LLMCompleter, *, max_candidates: int = 20) -> None:
        self._completer = completer
        self._max_candidates = max_candidates
        self._cache: dict[str, JudgeVerdict] = {}
        self.total_cost_usd = 0.0

    def judge(
        self, change: BreakingChange, call_sites: list[CallSite], root: Path
    ) -> list[CallSite]:
        by_file: dict[str, list[CallSite]] = {}
        for site in call_sites:
            by_file.setdefault(site.filepath, []).append(site)
        refined: dict[tuple[str, int], JudgeVerdict] = {}
        for relpath, sites in sorted(by_file.items(), key=lambda item: -len(item[1]))[
            : self._max_candidates
        ]:
            try:
                source = (root / relpath).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for verdict in self._judge_file(change, relpath, source, sites):
                refined[(verdict.filepath, verdict.line_number)] = verdict
        output: list[CallSite] = []
        for site in call_sites:
            verdict = refined.get((site.filepath, site.line_number))  # type: ignore[assignment]
            if verdict is None:
                output.append(site)
            elif verdict.affected:
                confidence = (
                    MatchConfidence.HIGH
                    if site.confidence is not MatchConfidence.LOW
                    else MatchConfidence.MEDIUM
                )
                output.append(
                    replace(
                        site,
                        confidence=confidence,
                        evidence=f"{site.evidence} + LLM judge: {verdict.why}".strip(" +"),
                    )
                )
        return output

    def _judge_file(
        self, change: BreakingChange, relpath: str, source: str, sites: list[CallSite]
    ) -> list[JudgeVerdict]:
        digest = hashlib.sha256(source.encode()).hexdigest()
        missing = [
            site
            for site in sites
            if self._key(digest, change.dedupe_key, site.line_number) not in self._cache
        ]
        if missing:
            prompt = (
                f"Old SDK API: {change.old_api}; replacement: {change.replacement}.\n"
                f"Only judge these lines in {relpath}: {[site.line_number for site in missing]}.\n"
                "Return JSON array of {line, affected, why}; never include other lines.\n\n"
                + redact_text(source)[:12000]
            )
            response = self._completer.complete(prompt, temperature=0.0)
            self.total_cost_usd += response.cost_estimate
            try:
                payload = json.loads(
                    response.text.strip().removeprefix("```json").removesuffix("```")
                )
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, list):
                wanted = {site.line_number for site in missing}
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    try:
                        line = int(item["line"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if line in wanted:
                        self._cache[self._key(digest, change.dedupe_key, line)] = JudgeVerdict(
                            relpath, line, bool(item.get("affected")), str(item.get("why") or "")
                        )
        return [
            self._cache[self._key(digest, change.dedupe_key, site.line_number)]
            for site in sites
            if self._key(digest, change.dedupe_key, site.line_number) in self._cache
        ]

    @staticmethod
    def _key(digest: str, dedupe_key: str, line: int) -> str:
        return f"{digest}:{dedupe_key}:{line}"
