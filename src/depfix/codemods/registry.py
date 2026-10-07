"""Selecting and running codemods.

The registry is ordered by specificity so a rule that understands one
library's whole migration beats a generic pattern that only understands the
rename. It is also the isolation boundary: a codemod that raises is
converted into a decline, because a bug in a deterministic rule must
degrade to "the model handles this file", never to a crashed run.
"""

from __future__ import annotations

import logging
from pathlib import Path

from depfix.codemods.base import Codemod, CodemodResult
from depfix.codemods.config_literal import ApiVersionLiteralCodemod, ConfigKeyCodemod
from depfix.codemods.javascript import (
    MethodRenameCodemod,
    OpenAIV4ConstructorCodemod,
    OpenAIV4MethodCodemod,
)
from depfix.core.models import BreakingChange

logger = logging.getLogger(__name__)

__all__ = ["CodemodRegistry"]


def _default_codemods() -> list[Codemod]:
    return [
        OpenAIV4ConstructorCodemod(),
        OpenAIV4MethodCodemod(),
        MethodRenameCodemod(),
        ApiVersionLiteralCodemod(),
        ConfigKeyCodemod(),
    ]


class CodemodRegistry:
    """Holds the available rules and picks one per change."""

    def __init__(self, codemods: list[Codemod] | None = None) -> None:
        pool = list(_default_codemods() if codemods is None else codemods)
        self._codemods = sorted(pool, key=lambda c: c.specificity, reverse=True)

    @property
    def codemods(self) -> tuple[Codemod, ...]:
        return tuple(self._codemods)

    def select(self, change: BreakingChange) -> Codemod | None:
        """The most specific rule that claims ``change``, or ``None``."""
        for codemod in self._codemods:
            try:
                if codemod.claims(change):
                    return codemod
            except Exception:
                logger.exception("codemod %s raised in claims(); ignoring it", codemod.id)
        return None

    def apply(self, relpath: str, source: str, change: BreakingChange) -> CodemodResult | None:
        """Run the selected rule against one file.

        ``None`` means no rule claimed this change at all, which is the
        normal case for arbitrary libraries and simply means the language
        model does the work.
        """
        codemod = self.select(change)
        if codemod is None:
            return None
        try:
            result = codemod.apply(source, change)
        except Exception as exc:
            logger.exception("codemod %s raised on %s", codemod.id, relpath)
            return CodemodResult.decline(
                codemod.id, f"the rule raised {type(exc).__name__} (this is a depfix bug)"
            )
        if result.changed and result.source is None:
            logger.error("codemod %s reported a change with no source", codemod.id)
            return CodemodResult.decline(
                codemod.id, "the rule reported a change but returned no source (depfix bug)"
            )
        return result

    def audit_cross_file(
        self, change: BreakingChange, checkout_root: Path, edited_relpaths: set[str]
    ) -> list[str]:
        """Report migrated symbols still found in source files we did not edit."""
        needles = _cross_file_needles(change)
        if not needles:
            return []
        warnings: list[str] = []
        extensions = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
        skip = {"node_modules", ".git", "dist", "build", "coverage", ".next"}
        for path in checkout_root.rglob("*"):
            if not path.is_file() or path.suffix not in extensions:
                continue
            relative = path.relative_to(checkout_root)
            relpath = relative.as_posix()
            if relpath in edited_relpaths or any(part in skip for part in relative.parts[:-1]):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            needle = next((item for item in needles if item in source), None)
            if needle:
                warnings.append(f"{relpath} still references `{needle}` -- may need manual review")
        return warnings

    def apply_batch(
        self, file_sources: dict[str, str], change: BreakingChange
    ) -> dict[str, CodemodResult]:
        """Apply all claiming codemods cumulatively to every candidate file.

        A migration may need a constructor rewrite and a method rename in the
        same wrapper.  Applying the ordered rules to one shared source avoids
        the former edit being discarded by the latter.
        """
        claimants = [rule for rule in self._codemods if self._safe_claims(rule, change)]
        results: dict[str, CodemodResult] = {}
        for relpath, original in file_sources.items():
            current = original
            notes: list[str] = []
            codemod_id = ""
            for rule in claimants:
                try:
                    result = rule.apply(current, change)
                except Exception:
                    logger.exception("codemod %s raised on %s in batch", rule.id, relpath)
                    continue
                if result.changed and result.source is not None:
                    current = result.source
                    codemod_id = result.codemod_id
                    if result.note:
                        notes.append(result.note)
                elif result.declined:
                    notes.append(f"{rule.id} declined: {result.declined_reason}")
            if current != original:
                results[relpath] = CodemodResult.rewrote(codemod_id, current, "; ".join(notes))
        return results

    @staticmethod
    def _safe_claims(codemod: Codemod, change: BreakingChange) -> bool:
        try:
            return codemod.claims(change)
        except Exception:
            logger.exception("codemod %s raised in claims()", codemod.id)
            return False


def _cross_file_needles(change: BreakingChange) -> list[str]:
    tail = change.old_api.strip().split(".")[-1].split("(")[0]
    needles = [tail] if len(tail) >= 4 and not tail.islower() else []
    if change.package.lower() == "openai":
        needles.extend(["Configuration", "OpenAIApi"])
    return list(dict.fromkeys(needles))
