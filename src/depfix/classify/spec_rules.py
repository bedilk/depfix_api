"""Deterministic ``BreakingChange`` derivation from OpenAPI structural diffs.

This is the only classification path that runs in CI without an API key:
every input is already a validated ``SpecChange`` (see
``sources.openapi_diff``), so there is nothing to hallucinate. Confidence is
always 1.0 and ``source`` is always ``ClassificationSource.SPEC_DIFF``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from depfix.core.models import (
    REMOVAL_KINDS,
    BreakingChange,
    ChangeKind,
    ClassificationSource,
)
from depfix.sources.models import Severity, SpecChange, SpecChangeKind

# (spec_change_kind, direction) -> ChangeKind. Direction matters: losing a
# response field breaks readers, losing a request field breaks writers, and
# the downstream fix differs even though the spec delta looks identical.
_KIND_MAP: dict[tuple[SpecChangeKind, str], ChangeKind] = {
    (SpecChangeKind.PATH_REMOVED, "n/a"): ChangeKind.METHOD_REMOVED,
    (SpecChangeKind.OPERATION_REMOVED, "n/a"): ChangeKind.METHOD_REMOVED,
    (SpecChangeKind.OPERATION_ID_CHANGED, "n/a"): ChangeKind.METHOD_RENAMED,
    (SpecChangeKind.PARAM_REMOVED, "request"): ChangeKind.PARAM_REMOVED,
    (SpecChangeKind.PARAM_NOW_REQUIRED, "request"): ChangeKind.PARAM_NOW_REQUIRED,
    (SpecChangeKind.PARAM_TYPE_CHANGED, "request"): ChangeKind.PARAM_TYPE_CHANGED,
    (SpecChangeKind.PROPERTY_REMOVED, "request"): ChangeKind.PARAM_REMOVED,
    (SpecChangeKind.PROPERTY_REMOVED, "response"): ChangeKind.FIELD_REMOVED,
    (SpecChangeKind.PROPERTY_NOW_REQUIRED, "request"): ChangeKind.PARAM_NOW_REQUIRED,
    (SpecChangeKind.PROPERTY_NOW_REQUIRED, "response"): ChangeKind.RESPONSE_SHAPE_CHANGED,
    (SpecChangeKind.PROPERTY_TYPE_CHANGED, "request"): ChangeKind.PARAM_TYPE_CHANGED,
    (SpecChangeKind.PROPERTY_TYPE_CHANGED, "response"): ChangeKind.FIELD_TYPE_CHANGED,
    (SpecChangeKind.ENUM_VALUE_REMOVED, "request"): ChangeKind.ENUM_VALUE_REMOVED,
    (SpecChangeKind.ENUM_VALUE_REMOVED, "response"): ChangeKind.ENUM_VALUE_REMOVED,
    (SpecChangeKind.SCHEMA_REMOVED, "n/a"): ChangeKind.RESPONSE_SHAPE_CHANGED,
    # AsyncAPI / Redis / TS-export kinds map onto the nearest existing ChangeKind.
    (SpecChangeKind.OPERATION_MESSAGE_CHANGED, "n/a"): ChangeKind.RESPONSE_SHAPE_CHANGED,
    (SpecChangeKind.COMMAND_REMOVED, "n/a"): ChangeKind.METHOD_REMOVED,
    (SpecChangeKind.COMMAND_ARITY_CHANGED, "n/a"): ChangeKind.PARAM_TYPE_CHANGED,
    (SpecChangeKind.EXPORT_REMOVED, "n/a"): ChangeKind.METHOD_REMOVED,
    (SpecChangeKind.OPERATION_DEPRECATED, "n/a"): ChangeKind.METHOD_DEPRECATED,
}

# Additive by construction — never worth surfacing as a breaking change
# regardless of how the differ scored its severity.
_IGNORED = {
    SpecChangeKind.OPERATION_ADDED,
    SpecChangeKind.PROPERTY_ADDED,
}


def _map_kind(change: SpecChange) -> ChangeKind | None:
    if change.kind in _IGNORED or change.severity is Severity.ADDITIVE:
        return None
    return _KIND_MAP.get((change.kind, change.direction)) or _KIND_MAP.get((change.kind, "n/a"))


def sdk_symbol_for(subject: str, *, provider_id: str = "") -> str | None:
    """The dotted SDK symbol a ``SpecChange`` subject refers to, or ``None``.

    Two shapes arrive here. Fixture and synthesis feeds already carry a
    dotted symbol (``openai.createChatCompletion``) and are returned
    verbatim. A real OpenAPI diff carries an HTTP subject
    (``POST /v1/charges``), which :func:`sdk_path_hints` turns into
    ``charges.create``.

    ``provider_id`` lets a hyphenated head (``google-cloud``, ``vercel-ai``)
    survive normalization; without it those subjects normalize to ``None``
    and silently lose every hyphenated provider's symbols.
    """
    from depfix.codemods.symbols import normalize_api

    normalized = normalize_api(subject, provider_id=provider_id)
    if normalized is not None:
        return normalized
    return sdk_path_hints(subject)


def sdk_path_hints(subject: str) -> str | None:
    """Best-effort ``METHOD /path`` -> ``resource.verb`` SDK-method guess.

    Generated SDKs (openai-node, stripe-node, ...) turn ``POST /v1/charges``
    into ``charges.create`` and ``GET /v1/charges/{id}`` into
    ``charges.retrieve``. This is a heuristic seed for ``call_site_hints``,
    not a guarantee — the scanner still has to find the real call site.
    """
    parts = subject.split(" ", 1)
    if len(parts) != 2:
        return None
    method, path = parts
    segments = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    if not segments:
        return None
    resource = segments[-1]
    has_trailing_param = path.rstrip("/").endswith("}")
    verb = {
        ("GET", False): "list",
        ("GET", True): "retrieve",
        ("POST", False): "create",
        ("POST", True): "update",
        ("PUT", False): "replace",
        ("PUT", True): "update",
        ("PATCH", False): "update",
        ("PATCH", True): "update",
        ("DELETE", False): "delete",
        ("DELETE", True): "delete",
    }.get((method.upper(), has_trailing_param))
    if verb is None:
        return None
    return f"{resource}.{verb}"


@dataclass(frozen=True)
class _Bucket:
    subject: str
    kind: ChangeKind
    direction: str


def _new_api_for(bucket: _Bucket, members: list[SpecChange], *, provider_id: str = "") -> str:
    if bucket.kind is ChangeKind.METHOD_RENAMED:
        after = next((m.after for m in members if m.after), None)
        if after:
            from depfix.codemods.symbols import normalize_api

            normalized = normalize_api(after, provider_id=provider_id)
            if normalized is not None:
                return normalized
            return after
        return sdk_symbol_for(bucket.subject, provider_id=provider_id) or bucket.subject
    if bucket.kind is ChangeKind.METHOD_DEPRECATED:
        after = next((m.after for m in members if m.after), None)
        if after:
            from depfix.codemods.symbols import normalize_api

            normalized = normalize_api(after, provider_id=provider_id)
            if normalized is not None:
                return normalized
            return after
        return ""
    if bucket.kind in REMOVAL_KINDS:
        return ""
    if bucket.kind is ChangeKind.PARAM_NOW_REQUIRED:
        names = [m.after for m in members if m.after]
        if names:
            return f"{bucket.subject} — now requires: {', '.join(dict.fromkeys(names))}"
        return f"{bucket.subject} — new required parameter"
    if bucket.kind in (ChangeKind.PARAM_TYPE_CHANGED, ChangeKind.FIELD_TYPE_CHANGED):
        pairs = [f"{m.before} -> {m.after}" for m in members if m.before and m.after]
        return f"{bucket.subject} — type change: {', '.join(pairs)}" if pairs else bucket.subject
    return bucket.subject


def _old_api_for(bucket: _Bucket, members: list[SpecChange], *, provider_id: str = "") -> str:
    """The thing that changed, as a call symbol where one is derivable."""
    if bucket.kind in (ChangeKind.METHOD_REMOVED, ChangeKind.METHOD_RENAMED):
        return sdk_symbol_for(bucket.subject, provider_id=provider_id) or bucket.subject
    removed_names = [m.before for m in members if m.before]
    if removed_names:
        return f"{bucket.subject} ({', '.join(dict.fromkeys(removed_names))})"
    return bucket.subject


def classify_spec_changes(
    changes: list[SpecChange],
    *,
    package: str,
    old_version: str,
    new_version: str,
    provider_id: str,
    source_url: str | None,
) -> list[BreakingChange]:
    """Group structural spec deltas into reviewer-sized ``BreakingChange`` rows.

    Multiple ``SpecChange`` rows that share ``(subject, mapped_kind,
    direction)`` collapse into one ``BreakingChange`` — a rewrite of one
    endpoint's request body shouldn't produce 40 separate rows.
    """
    buckets: dict[_Bucket, list[SpecChange]] = defaultdict(list)
    for change in changes:
        mapped = _map_kind(change)
        if mapped is None:
            continue
        buckets[_Bucket(change.subject, mapped, change.direction)].append(change)

    out: list[BreakingChange] = []
    for bucket, members in buckets.items():
        details = [m.detail for m in members if m.detail]
        hint = sdk_path_hints(bucket.subject)
        description = "; ".join(dict.fromkeys(details)) or bucket.subject
        if (
            bucket.kind in (ChangeKind.METHOD_REMOVED, ChangeKind.METHOD_RENAMED)
            and " " in bucket.subject
        ):
            # old_api is now a dotted symbol, so the HTTP endpoint it came from
            # must survive somewhere a reviewer will read it.
            description = f"{description} ({bucket.subject})"

        out.append(
            BreakingChange(
                package=package,
                old_version=old_version,
                new_version=new_version,
                old_api=_old_api_for(bucket, members, provider_id=provider_id),
                new_api=_new_api_for(bucket, members, provider_id=provider_id),
                description=description,
                migration_guide="",
                kind=bucket.kind,
                source=ClassificationSource.SPEC_DIFF,
                provider_id=provider_id,
                source_url=source_url,
                evidence=None,
                confidence=1.0,
                call_site_hints=[hint] if hint else [],
            )
        )
    return out
