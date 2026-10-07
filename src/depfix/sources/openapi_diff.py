"""Structural diff between two OpenAPI documents.

Pure function, no I/O, no network — which makes it the easiest thing in the
codebase to build the Week 2 eval corpus against.

Scope and known gaps (deliberate for Week 1):

* Local ``$ref`` only (``#/components/...``). Remote refs resolve to ``{}``.
* ``allOf`` is shallow-merged. ``oneOf`` / ``anyOf`` are *not* traversed —
  Stripe leans on ``anyOf`` heavily, so recall there is incomplete. Tracked as
  a Week 2 item; the eval corpus will tell us how much it costs.
* Recursion is depth-capped (schemas like Stripe's ``balance_transaction``
  self-reference) and the change list is count-capped so one spec rewrite
  cannot produce 40k rows.
"""

from __future__ import annotations

from typing import Any

from depfix.sources.models import Severity, SpecChange, SpecChangeKind

_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")

MAX_DEPTH = 6
MAX_CHANGES = 500


class _Resolver:
    """Resolves local ``$ref`` pointers, one hop at a time, cycle-safe."""

    def __init__(self, root: dict[str, Any]) -> None:
        self._root = root

    def resolve(self, node: Any) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {}
        seen: set[str] = set()
        current = node
        while isinstance(current, dict) and "$ref" in current:
            ref = current["$ref"]
            sibling = {k: v for k, v in current.items() if k != "$ref"}
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
                return sibling
            seen.add(ref)
            target = self._lookup(ref)
            if target is None:
                return sibling
            current = {**target, **sibling}
        return current if isinstance(current, dict) else {}

    def _lookup(self, ref: str) -> dict[str, Any] | None:
        cursor: Any = self._root
        for raw in ref[2:].split("/"):
            part = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            elif isinstance(cursor, list) and part.isdigit() and int(part) < len(cursor):
                cursor = cursor[int(part)]
            else:
                return None
        return cursor if isinstance(cursor, dict) else None


def _flatten_all_of(resolver: _Resolver, schema: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``allOf`` members into the parent schema."""
    members = schema.get("allOf")
    if not isinstance(members, list):
        return schema

    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    props: dict[str, Any] = dict(merged.get("properties") or {})
    required: list[Any] = list(merged.get("required") or [])

    for raw in members:
        member = _flatten_all_of(resolver, resolver.resolve(raw))
        props.update(member.get("properties") or {})
        required.extend(member.get("required") or [])
        for key, value in member.items():
            if key not in ("properties", "required") and key not in merged:
                merged[key] = value

    if props:
        merged["properties"] = props
    if required:
        merged["required"] = sorted(set(required), key=str)
    return merged


def _type_of(schema: dict[str, Any]) -> str | None:
    t = schema.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):  # OpenAPI 3.1 union types
        return "|".join(sorted(str(x) for x in t))
    return None


def diff_specs(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    max_depth: int = MAX_DEPTH,
    max_changes: int = MAX_CHANGES,
) -> list[SpecChange]:
    """Return the structural deltas from ``old`` to ``new``."""
    old_r, new_r = _Resolver(old), _Resolver(new)
    changes: list[SpecChange] = []

    _diff_paths(old, new, old_r, new_r, changes, max_depth, max_changes)
    _diff_components(old, new, changes, max_changes)
    return changes[:max_changes]


# --- paths / operations ------------------------------------------------------


def _diff_paths(
    old: dict[str, Any],
    new: dict[str, Any],
    old_r: _Resolver,
    new_r: _Resolver,
    changes: list[SpecChange],
    max_depth: int,
    max_changes: int,
) -> None:
    old_paths = old.get("paths") or {}
    new_paths = new.get("paths") or {}
    if not isinstance(old_paths, dict) or not isinstance(new_paths, dict):
        return

    for path in old_paths:
        if len(changes) >= max_changes:
            return
        if path not in new_paths:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PATH_REMOVED,
                    severity=Severity.BREAKING,
                    subject=str(path),
                    pointer=f"paths.{path}",
                    detail="endpoint no longer present in the spec",
                    before=str(path),
                )
            )

    for path in sorted(set(old_paths) & set(new_paths), key=str):
        if len(changes) >= max_changes:
            return
        old_item = old_r.resolve(old_paths[path])
        new_item = new_r.resolve(new_paths[path])

        for method in _METHODS:
            old_op = old_item.get(method)
            new_op = new_item.get(method)
            subject = f"{method.upper()} {path}"
            base = f"paths.{path}.{method}"

            if isinstance(old_op, dict) and not isinstance(new_op, dict):
                changes.append(
                    SpecChange(
                        kind=SpecChangeKind.OPERATION_REMOVED,
                        severity=Severity.BREAKING,
                        subject=subject,
                        pointer=base,
                        detail="operation removed",
                    )
                )
            elif isinstance(new_op, dict) and not isinstance(old_op, dict):
                changes.append(
                    SpecChange(
                        kind=SpecChangeKind.OPERATION_ADDED,
                        severity=Severity.ADDITIVE,
                        subject=subject,
                        pointer=base,
                        detail="new operation",
                    )
                )
            elif isinstance(old_op, dict) and isinstance(new_op, dict):
                _diff_operation(
                    old_item,
                    new_item,
                    old_op,
                    new_op,
                    old_r,
                    new_r,
                    subject,
                    base,
                    changes,
                    max_depth,
                    max_changes,
                )


def _diff_operation(
    old_item: dict[str, Any],
    new_item: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    old_r: _Resolver,
    new_r: _Resolver,
    subject: str,
    base: str,
    changes: list[SpecChange],
    max_depth: int,
    max_changes: int,
) -> None:
    old_id, new_id = old_op.get("operationId"), new_op.get("operationId")
    if old_id and new_id and old_id != new_id:
        # Generated SDKs name methods off operationId — a rename silently
        # deletes a method from every client.
        changes.append(
            SpecChange(
                kind=SpecChangeKind.OPERATION_ID_CHANGED,
                severity=Severity.POTENTIALLY_BREAKING,
                subject=subject,
                pointer=f"{base}.operationId",
                detail="operationId renamed — generated SDK method names change",
                before=str(old_id),
                after=str(new_id),
            )
        )

    if not old_op.get("deprecated") and new_op.get("deprecated"):
        changes.append(
            SpecChange(
                kind=SpecChangeKind.OPERATION_DEPRECATED,
                severity=Severity.POTENTIALLY_BREAKING,
                subject=subject,
                pointer=f"{base}.deprecated",
                detail="operation newly marked deprecated",
            )
        )

    _diff_params(
        old_item, new_item, old_op, new_op, old_r, new_r, subject, base, changes, max_changes
    )

    old_body = _body_schema(old_r, old_op)
    new_body = _body_schema(new_r, new_op)
    if old_body or new_body:
        _diff_schema(
            old_body,
            new_body,
            old_r,
            new_r,
            "request",
            subject,
            f"{base}.requestBody",
            changes,
            0,
            max_depth,
            max_changes,
        )

    old_resp = _response_schema(old_r, old_op)
    new_resp = _response_schema(new_r, new_op)
    if old_resp or new_resp:
        _diff_schema(
            old_resp,
            new_resp,
            old_r,
            new_r,
            "response",
            subject,
            f"{base}.response",
            changes,
            0,
            max_depth,
            max_changes,
        )


def _params_by_key(
    resolver: _Resolver, path_item: dict[str, Any], operation: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    raw_params = list(path_item.get("parameters") or []) + list(operation.get("parameters") or [])
    for raw in raw_params:
        param = resolver.resolve(raw)
        name, location = param.get("name"), param.get("in")
        if isinstance(name, str) and isinstance(location, str):
            out[(location, name)] = param
    return out


def _diff_params(
    old_item: dict[str, Any],
    new_item: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    old_r: _Resolver,
    new_r: _Resolver,
    subject: str,
    base: str,
    changes: list[SpecChange],
    max_changes: int,
) -> None:
    old_params = _params_by_key(old_r, old_item, old_op)
    new_params = _params_by_key(new_r, new_item, new_op)

    for key in old_params:
        if len(changes) >= max_changes:
            return
        if key not in new_params:
            location, name = key
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PARAM_REMOVED,
                    severity=Severity.BREAKING,
                    subject=subject,
                    pointer=f"{base}.parameters.{location}.{name}",
                    detail=f"{location} parameter '{name}' removed",
                    direction="request",
                    before=name,
                )
            )

    for key, new_param in new_params.items():
        if len(changes) >= max_changes:
            return
        location, name = key
        old_param = old_params.get(key)
        pointer = f"{base}.parameters.{location}.{name}"

        if old_param is None:
            if new_param.get("required"):
                changes.append(
                    SpecChange(
                        kind=SpecChangeKind.PARAM_NOW_REQUIRED,
                        severity=Severity.BREAKING,
                        subject=subject,
                        pointer=pointer,
                        detail=f"new required {location} parameter '{name}'",
                        direction="request",
                        after=name,
                    )
                )
            continue

        if not old_param.get("required") and new_param.get("required"):
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PARAM_NOW_REQUIRED,
                    severity=Severity.BREAKING,
                    subject=subject,
                    pointer=pointer,
                    detail=f"{location} parameter '{name}' is now required",
                    direction="request",
                )
            )

        old_type = _type_of(old_r.resolve(old_param.get("schema") or {}))
        new_type = _type_of(new_r.resolve(new_param.get("schema") or {}))
        if old_type and new_type and old_type != new_type:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PARAM_TYPE_CHANGED,
                    severity=Severity.BREAKING,
                    subject=subject,
                    pointer=pointer,
                    detail=f"parameter '{name}' type {old_type} -> {new_type}",
                    direction="request",
                    before=old_type,
                    after=new_type,
                )
            )


# --- schemas ----------------------------------------------------------------


def _body_schema(resolver: _Resolver, operation: dict[str, Any]) -> dict[str, Any]:
    body = resolver.resolve(operation.get("requestBody") or {})
    content = body.get("content")
    if not isinstance(content, dict):
        return {}
    for ct in ("application/json", "application/x-www-form-urlencoded"):
        if ct in content:
            media = content[ct] or {}
            return _flatten_all_of(resolver, resolver.resolve(media.get("schema") or {}))
    for ct, media in content.items():
        if "json" in str(ct):
            return _flatten_all_of(resolver, resolver.resolve((media or {}).get("schema") or {}))
    return {}


def _response_schema(resolver: _Resolver, operation: dict[str, Any]) -> dict[str, Any]:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return {}
    codes = sorted(str(c) for c in responses if str(c).startswith("2"))
    if not codes:
        return {}
    resp = resolver.resolve(responses.get(codes[0], {}))
    if not resp:
        for key in responses:
            if str(key) == codes[0]:
                resp = resolver.resolve(responses[key])
                break
    content = resp.get("content")
    if not isinstance(content, dict):
        return {}
    for ct, media in content.items():
        if "json" in str(ct):
            return _flatten_all_of(resolver, resolver.resolve((media or {}).get("schema") or {}))
    return {}


def _diff_schema(
    old: dict[str, Any],
    new: dict[str, Any],
    old_r: _Resolver,
    new_r: _Resolver,
    direction: str,
    subject: str,
    pointer: str,
    changes: list[SpecChange],
    depth: int,
    max_depth: int,
    max_changes: int,
) -> None:
    if depth > max_depth or len(changes) >= max_changes:
        return

    old = _flatten_all_of(old_r, old)
    new = _flatten_all_of(new_r, new)

    old_type, new_type = _type_of(old), _type_of(new)
    if old_type and new_type and old_type != new_type:
        changes.append(
            SpecChange(
                kind=SpecChangeKind.PROPERTY_TYPE_CHANGED,
                severity=Severity.BREAKING,
                subject=subject,
                pointer=pointer,
                detail=f"type {old_type} -> {new_type}",
                direction=direction,
                before=old_type,
                after=new_type,
            )
        )

    old_enum, new_enum = old.get("enum"), new.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        gone = [v for v in old_enum if v not in new_enum]
        if gone:
            # Removing an accepted request value breaks callers who send it.
            # Removing a returned value only breaks exhaustive switches.
            severity = (
                Severity.BREAKING if direction == "request" else Severity.POTENTIALLY_BREAKING
            )
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.ENUM_VALUE_REMOVED,
                    severity=severity,
                    subject=subject,
                    pointer=pointer,
                    detail="enum values removed: " + ", ".join(str(v) for v in gone[:8]),
                    direction=direction,
                    before=", ".join(str(v) for v in gone[:8]),
                )
            )

    old_items, new_items = old.get("items"), new.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        _diff_schema(
            old_r.resolve(old_items),
            new_r.resolve(new_items),
            old_r,
            new_r,
            direction,
            subject,
            f"{pointer}[]",
            changes,
            depth + 1,
            max_depth,
            max_changes,
        )

    old_props = old.get("properties") or {}
    new_props = new.get("properties") or {}
    if not isinstance(old_props, dict) or not isinstance(new_props, dict):
        return
    old_required = {str(x) for x in (old.get("required") or [])}
    new_required = {str(x) for x in (new.get("required") or [])}

    for name in old_props:
        if len(changes) >= max_changes:
            return
        if name not in new_props:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PROPERTY_REMOVED,
                    severity=Severity.BREAKING,
                    subject=subject,
                    pointer=f"{pointer}.{name}",
                    detail=f"{direction} field '{name}' removed",
                    direction=direction,
                    before=str(name),
                )
            )

    for name in new_props:
        if len(changes) >= max_changes:
            return
        if name in old_props:
            continue
        if direction == "request" and name in new_required:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PROPERTY_NOW_REQUIRED,
                    severity=Severity.BREAKING,
                    subject=subject,
                    pointer=f"{pointer}.{name}",
                    detail=f"new required request field '{name}'",
                    direction=direction,
                    after=str(name),
                )
            )
        else:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PROPERTY_ADDED,
                    severity=Severity.ADDITIVE,
                    subject=subject,
                    pointer=f"{pointer}.{name}",
                    detail=f"new {direction} field '{name}'",
                    direction=direction,
                    after=str(name),
                )
            )

    for name in sorted(set(old_props) & set(new_props), key=str):
        if len(changes) >= max_changes:
            return
        if direction == "request" and name not in old_required and name in new_required:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.PROPERTY_NOW_REQUIRED,
                    severity=Severity.BREAKING,
                    subject=subject,
                    pointer=f"{pointer}.{name}",
                    detail=f"request field '{name}' is now required",
                    direction=direction,
                )
            )
        _diff_schema(
            old_r.resolve(old_props[name]),
            new_r.resolve(new_props[name]),
            old_r,
            new_r,
            direction,
            subject,
            f"{pointer}.{name}",
            changes,
            depth + 1,
            max_depth,
            max_changes,
        )


def _diff_components(
    old: dict[str, Any],
    new: dict[str, Any],
    changes: list[SpecChange],
    max_changes: int,
) -> None:
    old_schemas = ((old.get("components") or {}).get("schemas")) or {}
    new_schemas = ((new.get("components") or {}).get("schemas")) or {}
    if not isinstance(old_schemas, dict) or not isinstance(new_schemas, dict):
        return
    for name in old_schemas:
        if len(changes) >= max_changes:
            return
        if name not in new_schemas:
            changes.append(
                SpecChange(
                    kind=SpecChangeKind.SCHEMA_REMOVED,
                    severity=Severity.POTENTIALLY_BREAKING,
                    subject=str(name),
                    pointer=f"components.schemas.{name}",
                    detail="schema removed — generated types disappear",
                    before=str(name),
                )
            )
