"""Redis command-table feed.

``redis/redis-doc/commands.json`` is a flat, machine-readable map of every
Redis command to its arguments and flags. Diff = removed commands and changed
argument counts.

Argument *renames* within the same count are not chased — they rarely change
the client call and modelling Redis's nested/optional argument grammar is more
machinery than the signal is worth.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from depfix.sources.models import Severity, SourceKind, SpecChange, SpecChangeKind
from depfix.sources.spec_base import StructuredSpecSource


class RedisCommandsSource(StructuredSpecSource):
    kind: ClassVar[SourceKind] = SourceKind.REDIS_COMMANDS
    spec_label: ClassVar[str] = "redis-commands"

    def parse(self, raw: bytes) -> dict[str, Any]:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"commands.json is not valid JSON: {exc}") from exc
        if not isinstance(document, dict) or not document:
            raise ValueError("commands.json is not a non-empty command map")
        return {"commands": document}

    def version_of(self, document: dict[str, Any], sha: str) -> str:
        return f"cmds-{len(document.get('commands') or {})}"

    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        old_cmds = old.get("commands") or {}
        new_cmds = new.get("commands") or {}
        changes: list[SpecChange] = []

        for name in sorted(set(old_cmds) - set(new_cmds)):
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.COMMAND_REMOVED,
                    severity=Severity.BREAKING,
                    subject=name,
                    pointer=f"commands.{name}",
                    detail="command removed from the Redis command table",
                    before=name,
                )
            )

        for name in sorted(set(old_cmds) & set(new_cmds)):
            old_arity = self._arg_count(old_cmds[name])
            new_arity = self._arg_count(new_cmds[name])
            if old_arity is not None and new_arity is not None and old_arity != new_arity:
                changes.append(
                    SpecChange(
                        kind=SpecChangeKind.COMMAND_ARITY_CHANGED,
                        severity=Severity.POTENTIALLY_BREAKING,
                        subject=name,
                        pointer=f"commands.{name}.arguments",
                        detail=f"argument count {old_arity} -> {new_arity}",
                        before=str(old_arity),
                        after=str(new_arity),
                    )
                )
        return changes

    @staticmethod
    def _arg_count(command: Any) -> int | None:
        if not isinstance(command, dict):
            return None
        args = command.get("arguments")
        return len(args) if isinstance(args, list) else None
