"""JSON (de)serialization for :class:`depfix.core.models.BreakingChange`.

Three call sites need the same conversion: ``depfix run/fix --change-file``
(the CLI), the eval corpus loader, and -- as of the self-describing plan
artifact (:mod:`depfix.orchestrator.plan`) -- ``depfix plan``/``apply``.
Keeping one implementation here is what makes an artifact written by one
code path round-trip through another; a second, slightly-different copy of
this mapping would silently drop a field (``examples`` is the easy one to
forget, since it's the only tuple-valued one) and an ``apply`` would then
migrate against a subtly different change than the one that was reviewed.
"""

from __future__ import annotations

from typing import Any

from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource


def breaking_change_from_dict(data: dict[str, Any]) -> BreakingChange:
    """Rebuild a ``BreakingChange`` from its JSON form.

    Raises ``ValueError``/``TypeError`` on malformed input rather than
    coercing -- a plan artifact or ``--change-file`` that can't be read
    exactly is not something to guess at.
    """
    payload = dict(data)
    if "kind" in payload:
        payload["kind"] = ChangeKind(payload["kind"])
    if "source" in payload:
        payload["source"] = ClassificationSource(payload["source"])
    if payload.get("examples"):
        payload["examples"] = [tuple(pair) for pair in payload["examples"]]
    return BreakingChange(**payload)


def breaking_change_to_dict(change: BreakingChange) -> dict[str, Any]:
    """The inverse of :func:`breaking_change_from_dict`.

    Written out field-by-field rather than via ``dataclasses.asdict`` so
    that adding a field to ``BreakingChange`` without deciding whether it
    belongs in a persisted artifact is a visible omission here, not a
    silent schema change to every plan file ever written.
    """
    return {
        "package": change.package,
        "old_version": change.old_version,
        "new_version": change.new_version,
        "old_api": change.old_api,
        "new_api": change.new_api,
        "description": change.description,
        "migration_guide": change.migration_guide,
        "kind": change.kind.value,
        "source": change.source.value,
        "provider_id": change.provider_id,
        "source_url": change.source_url,
        "evidence": change.evidence,
        "confidence": change.confidence,
        "call_site_hints": list(change.call_site_hints),
        "examples": [list(pair) for pair in change.examples],
    }
