"""AWS Smithy model feed.

Smithy is AWS's own IDL. The JSON serialization is a flat ``shapes`` map from
shape id (``com.amazonaws.s3#PutObject``) to a shape node; an operation shape
points at ``input``/``output``/``errors`` targets, and a structure shape lists
``members``.

Scope, stated honestly: this diffs the *service* surface (operations and their
input members). It is the right signal for "did AWS remove or reshape an S3
operation," and it is a weaker match for ``@aws-sdk/client-s3`` call sites
than Stripe's OpenAPI is for the Stripe SDK, because the JS SDK's method names
are codegen-derived rather than 1:1 with shape ids. Reachability filtering
keeps that from producing noise.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from depfix.sources.models import Severity, SourceKind, SpecChange, SpecChangeKind
from depfix.sources.spec_base import StructuredSpecSource


def _shape_type(shape: dict[str, Any]) -> str:
    return str(shape.get("type") or "")


def _local(shape_id: str) -> str:
    """``com.amazonaws.s3#PutObject`` -> ``PutObject``."""
    return shape_id.rsplit("#", 1)[-1]


class SmithySpecSource(StructuredSpecSource):
    kind: ClassVar[SourceKind] = SourceKind.SMITHY_SPEC
    spec_label: ClassVar[str] = "smithy"

    def parse(self, raw: bytes) -> dict[str, Any]:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Smithy model is not valid JSON: {exc}") from exc
        if not isinstance(document, dict) or "shapes" not in document:
            raise ValueError("Smithy model has no top-level 'shapes' map")
        return document

    def version_of(self, document: dict[str, Any], sha: str) -> str:
        ops = sum(
            1 for s in (document.get("shapes") or {}).values() if _shape_type(s) == "operation"
        )
        return f"ops-{ops}"

    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        old_shapes = old.get("shapes") or {}
        new_shapes = new.get("shapes") or {}
        changes: list[SpecChange] = []

        old_ops = {sid for sid, s in old_shapes.items() if _shape_type(s) == "operation"}
        new_ops = {sid for sid, s in new_shapes.items() if _shape_type(s) == "operation"}

        for shape_id in sorted(old_ops - new_ops):
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.OPERATION_REMOVED,
                    severity=Severity.BREAKING,
                    subject=_local(shape_id),
                    pointer=shape_id,
                    detail="operation removed from the service model",
                    before=_local(shape_id),
                )
            )

        for shape_id in sorted(old_ops & new_ops):
            changes.extend(self._diff_operation_input(shape_id, old_shapes, new_shapes))

        return changes

    def _diff_operation_input(
        self, op_id: str, old_shapes: dict[str, Any], new_shapes: dict[str, Any]
    ) -> list[SpecChange]:
        old_members = self._input_members(op_id, old_shapes)
        new_members = self._input_members(op_id, new_shapes)
        removed = old_members - new_members
        if not removed:
            return []
        return [
            SpecChange(
                kind=SpecChangeKind.PARAM_REMOVED,
                severity=Severity.BREAKING,
                subject=_local(op_id),
                pointer=f"{op_id}.input.{name}",
                detail=f"input member '{name}' removed",
                direction="request",
                before=name,
            )
            for name in sorted(removed)
        ]

    @staticmethod
    def _input_members(op_id: str, shapes: dict[str, Any]) -> set[str]:
        operation = shapes.get(op_id) or {}
        input_target = (operation.get("input") or {}).get("target")
        if not input_target:
            return set()
        input_shape = shapes.get(input_target) or {}
        members = input_shape.get("members")
        return set(members) if isinstance(members, dict) else set()
