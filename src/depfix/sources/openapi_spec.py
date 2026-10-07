"""OpenAPI document feed — rebased on StructuredSpecSource.

State is the sha256 of the raw bytes, not an ETag — providers serving specs
off raw.githubusercontent don't send useful validators, and a content hash is
correct regardless.

The previous document is cached on disk (``SPEC_CACHE_DIR``) rather than in
Postgres: Stripe's spec3.json is ~6 MB and we need the *whole* prior document
to diff, so a blob column would be both large and useless for querying.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import yaml

from depfix.sources.models import SourceKind, SpecChange
from depfix.sources.openapi_diff import diff_specs
from depfix.sources.spec_base import StructuredSpecSource


def _dig(doc: dict[str, Any], *keys: str) -> Any:
    cursor: Any = doc
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


class OpenApiSpecSource(StructuredSpecSource):
    kind: ClassVar[SourceKind] = SourceKind.OPENAPI_SPEC
    spec_label: ClassVar[str] = "openapi"

    def parse(self, raw: bytes) -> dict[str, Any]:
        text = raw.decode("utf-8", errors="replace")
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ValueError(f"spec is neither JSON nor YAML: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("spec root is not a mapping")
        return document

    def version_of(self, document: dict[str, Any], sha: str) -> str:
        return str(_dig(document, "info", "version") or f"sha-{sha[:12]}")

    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        return diff_specs(old, new)
