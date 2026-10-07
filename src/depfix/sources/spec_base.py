"""Shared machinery for every feed that diffs a downloaded structured spec.

``openapi_spec`` grew fetch-cache-hash-baseline-diff inline; four more feed
kinds (Smithy, AsyncAPI, Redis commands, TypeScript exports) want the exact
same lifecycle and differ only in *what document they parse* and *how they
diff two of them*. So that lifecycle lives here once, and a concrete source
supplies two things: :meth:`parse` (bytes -> comparable dict) and
:meth:`diff` (old, new -> SpecChange list).

The invariants every subclass inherits for free:

* **Content-hash detection, not ETags.** Providers are inconsistent about
  serving correct validators for raw GitHub URLs; a sha256 of the body is
  always correct.
* **Baseline on first sight.** The first poll has nothing to diff against, so
  it records state and emits no event.
* **Size-capped fetch.** A spec URL is admin-trusted config, but the body is
  bounded.
* **Prior document cached on disk, not in the DB.** The whole prior document
  is needed to diff and is never queried by column.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import httpx

from depfix.sources.base import ChangeSource
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SpecChange,
    max_severity,
)

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value).strip("-")[:120] or "feed"


class StructuredSpecSource(ChangeSource):
    """Base for any feed that fetches one URL, parses it, and structurally
    diffs the parsed form against the previous poll's."""

    spec_label: ClassVar[str] = "spec"

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any],
        http: httpx.Client,
        cache_dir: Path,
        max_bytes: int,
    ) -> None:
        super().__init__(provider_id, config)
        self.url: str = self._require("url")
        self._http = http
        self._cache_dir = cache_dir
        self._max_bytes = max_bytes

    @property
    def feed_key(self) -> str:
        return f"{self.spec_label}:{self.url}"

    @abstractmethod
    def parse(self, raw: bytes) -> dict[str, Any]:
        """Decode ``raw`` into the comparable form :meth:`diff` expects.
        Raise ``ValueError`` for anything unparseable.
        """

    @abstractmethod
    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[SpecChange]:
        """Structural deltas from ``old`` to ``new``."""

    def version_of(self, document: dict[str, Any], sha: str) -> str:
        return f"sha-{sha[:12]}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            response = self._http.get(self.url)
        except httpx.HTTPError as exc:
            return FeedPoll.failure(f"fetch failed: {exc}")
        if response.status_code == 404:
            return FeedPoll.missing(f"{self.spec_label} URL 404: {self.url}")
        if response.status_code >= 400:
            return FeedPoll.failure(f"HTTP {response.status_code} for {self.url}")

        raw = response.content
        if len(raw) > self._max_bytes:
            return FeedPoll.failure(
                f"{self.spec_label} is {len(raw)} bytes, exceeds MAX_SPEC_BYTES={self._max_bytes}"
            )

        sha = hashlib.sha256(raw).hexdigest()
        if sha == state.last_payload_sha256:
            return FeedPoll.unchanged(token=state.last_token, sha=sha, path=state.last_payload_path)

        try:
            document = self.parse(raw)
        except ValueError as exc:
            return FeedPoll.failure(str(exc))

        version = self.version_of(document, sha)
        previous = self._load_cache(state.last_payload_path)
        cache_path = self._write_cache(sha, document)
        if previous is None:
            logger.info("%s baseline for %s (%s)", self.spec_label, self.provider_id, version)
            return FeedPoll.baseline(version, sha=sha, path=str(cache_path))

        changes = self.diff(previous, document)
        severity = max_severity([c.severity for c in changes])
        breaking = [c for c in changes if c.severity is Severity.BREAKING]
        summary = (
            f"{len(changes)} structural change(s), {len(breaking)} breaking"
            if changes
            else f"{self.spec_label} bytes changed but no structural delta detected"
        )
        event = ChangeEvent(
            provider_id=self.provider_id,
            feed_key=self.feed_key,
            source_kind=self.kind,
            source_url=self.url,
            old_token=state.last_token,
            new_token=version,
            title=f"{self.provider_id} {self.spec_label} {state.last_token} → {version}",
            summary=summary,
            body="\n".join(c.one_line() for c in changes[:60]),
            severity=severity,
            spec_changes=changes,
            raw={"sha256": sha, "change_count": len(changes)},
        )
        return FeedPoll(
            changed=True,
            new_token=version,
            payload_sha256=sha,
            payload_path=str(cache_path),
            events=[event],
        )

    def _feed_dir(self) -> Path:
        return self._cache_dir / _slug(self.provider_id) / _slug(self.feed_key)

    def _write_cache(self, sha: str, document: dict[str, Any]) -> Path:
        directory = self._feed_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sha}.json"
        path.write_text(json.dumps(document, separators=(",", ":"), default=str), encoding="utf-8")
        for stale in directory.glob("*.json"):
            if stale.name != path.name:
                stale.unlink(missing_ok=True)
        return path

    def _load_cache(self, path: str | None) -> dict[str, Any] | None:
        if not path:
            return None
        candidate = Path(path)
        if not candidate.exists():
            logger.warning("cached %s missing at %s — treating as baseline", self.spec_label, path)
            return None
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cached %s unreadable (%s) — treating as baseline", self.spec_label, exc)
            return None
        return loaded if isinstance(loaded, dict) else None
