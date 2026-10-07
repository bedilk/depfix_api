"""AsyncAPI feed for event-driven / messaging APIs.

AsyncAPI is the OpenAPI equivalent for brokers. This diffs channels and
operations across two versions of a published AsyncAPI document, handling
both the 2.x shape (``channels.<name>.{publish,subscribe}``) and the 3.x
shape (top-level ``operations`` referencing ``channels``).

Honest caveat: an AsyncAPI document describes the **broker's** surface
(Confluent Schema Registry, RabbitMQ's management/messaging API), not the
``kafkajs`` or ``amqplib`` *client library* whose call sites depfix scans.
A removed channel is a genuine breaking change to a service that talks to
that broker — but it will rarely line up with a client method the way
Stripe's OpenAPI lines up with the Stripe SDK. Reachability filtering keeps
it from becoming noise.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import yaml

from depfix.sources.models import Severity, SourceKind, SpecChange, SpecChangeKind
from depfix.sources.spec_base import StructuredSpecSource


class AsyncApiSpecSource(StructuredSpecSource):
    kind: ClassVar[SourceKind] = SourceKind.ASYNCAPI_SPEC
    spec_label: ClassVar[str] = "asyncapi"

    def parse(self, raw: bytes) -> dict[str, Any]:
        text = raw.decode("utf-8", errors="replace")
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ValueError(f"AsyncAPI doc is neither JSON nor YAML: {exc}") from exc
        if not isinstance(document, dict) or "asyncapi" not in document:
            raise ValueError("document has no top-level 'asyncapi' version field")
        return document

    def version_of(self, document: dict[str, Any], sha: str) -> str:
        info = document.get("info") or {}
        return str(info.get("version") or f"sha-{sha[:12]}")

    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        changes: list[SpecChange] = []
        changes.extend(self._diff_channels(old, new))
        changes.extend(self._diff_operations_v3(old, new))
        return changes

    def _diff_channels(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        old_channels = old.get("channels") or {}
        new_channels = new.get("channels") or {}
        if not isinstance(old_channels, dict) or not isinstance(new_channels, dict):
            return []
        return [
            SpecChange(
                kind=SpecChangeKind.OPERATION_REMOVED,
                severity=Severity.BREAKING,
                subject=name,
                pointer=f"channels.{name}",
                detail="channel removed",
                before=name,
            )
            for name in sorted(set(old_channels) - set(new_channels))
        ]

    def _diff_operations_v3(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        old_ops = old.get("operations") or {}
        new_ops = new.get("operations") or {}
        if not isinstance(old_ops, dict) or not isinstance(new_ops, dict):
            return []
        return [
            SpecChange(
                kind=SpecChangeKind.OPERATION_REMOVED,
                severity=Severity.BREAKING,
                subject=name,
                pointer=f"operations.{name}",
                detail="operation removed",
                before=name,
            )
            for name in sorted(set(old_ops) - set(new_ops))
        ]
