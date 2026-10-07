"""Safe codemods for dated API-version literals and configuration keys."""

from __future__ import annotations

import re

from depfix.codemods.base import CodemodResult
from depfix.core.models import BreakingChange, ChangeKind


class ApiVersionLiteralCodemod:
    id = "config.api-version-literal"
    specificity = 30
    _date = re.compile(r"\d{4}-\d{2}-\d{2}(?:-[\w.]+)?$")

    def claims(self, change: BreakingChange) -> bool:
        return (
            change.kind == ChangeKind.API_VERSION_BUMP
            and bool(self._date.fullmatch(change.old_api.strip()))
            and bool(self._date.fullmatch(change.new_api.strip()))
        )

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        old, new = change.old_api.strip(), change.new_api.strip()
        pattern = re.compile(
            rf"((?:apiVersion|api_version|Stripe-Version)\s*[:=]\s*)(['\"])({re.escape(old)})\2"
        )
        replaced, count = pattern.subn(
            lambda m: f"{m.group(1)}{m.group(2)}{new}{m.group(2)}", source
        )
        return (
            CodemodResult.rewrote(
                self.id, replaced, f"updated API version {old} -> {new} in {count} location(s)"
            )
            if count
            else CodemodResult.unchanged(self.id, "no API-version literal found")
        )


class ConfigKeyCodemod:
    id = "config.key-rename"
    specificity = 20

    def claims(self, change: BreakingChange) -> bool:
        return (
            change.kind in (ChangeKind.PARAM_RENAMED, ChangeKind.FIELD_RENAMED)
            and bool(re.fullmatch(r"[A-Za-z_]\w*", change.old_api.strip()))
            and bool(re.fullmatch(r"[A-Za-z_]\w*", change.new_api.strip()))
        )

    def apply(self, source: str, change: BreakingChange) -> CodemodResult:
        old, new = change.old_api.strip(), change.new_api.strip()
        # Require the identifier to sit where an object-literal key can --
        # right after `{`, `,`, `(`, or a line start -- so a same-named
        # ternary reference (`cond ? old : val`) or a mention inside a
        # comment/string is left alone. Not a full parse, but it rules out
        # the cheapest false positives a bare `\bold\s*:` would catch.
        pattern = re.compile(rf"(^|[{{,(\n])(\s*){re.escape(old)}(\s*:)", re.MULTILINE)
        result, count = pattern.subn(rf"\1\2{new}\3", source)
        return (
            CodemodResult.rewrote(self.id, result, f"renamed config key {old} -> {new}")
            if count
            else CodemodResult.unchanged(self.id, "no config key found")
        )
