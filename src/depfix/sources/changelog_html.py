"""HTML changelog feed for dated API-version announcements."""

from __future__ import annotations

import hashlib
import re
from typing import Any, ClassVar

import httpx

from depfix.sources.base import ChangeSource
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
)

#: Cap on how much of a changelog page we'll buffer in memory. A changelog
#: URL comes from admin-trusted config (``providers.yaml``/``--providers-file``),
#: but that config can itself point at an attacker-influenced host, and
#: without a cap a large or slow-drip response would otherwise be read to
#: completion (unlike :class:`depfix.clone.CloneService`, which enforces
#: ``max_repo_mb`` for the same reason on untrusted checkouts).
_MAX_RESPONSE_BYTES = 2_000_000

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_BREAKING_MARKERS = ("breaking", "removed", "deprecated", "migration")


class ChangelogHtmlSource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.CHANGELOG_HTML

    def __init__(self, provider_id: str, config: dict[str, Any], http: httpx.Client) -> None:
        super().__init__(provider_id, config)
        self.url = self._require("url")
        self._http = http

    @property
    def feed_key(self) -> str:
        return f"changelog_html:{self.url}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            with self._http.stream("GET", self.url) as response:
                if response.status_code == 404:
                    return FeedPoll.missing("changelog URL 404")
                if response.status_code >= 400:
                    return FeedPoll.failure(f"HTTP {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_RESPONSE_BYTES:
                        break
                raw = b"".join(chunks)[:_MAX_RESPONSE_BYTES]
                text = raw.decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            return FeedPoll.failure(f"fetch failed: {exc}")

        digest = hashlib.sha256(raw).hexdigest()
        versions = sorted(set(_DATE_RE.findall(text)), reverse=True)
        if not versions:
            return FeedPoll.baseline(state.last_token or "unknown", sha=digest)

        newest = versions[0]
        if state.last_token is None:
            return FeedPoll.baseline(newest, sha=digest)
        if newest == state.last_token:
            return FeedPoll.unchanged(token=newest, sha=digest)

        stripped = _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", text))
        index = stripped.find(newest)
        body = stripped[max(0, index - 500) : index + 1500]
        severity = (
            Severity.POTENTIALLY_BREAKING
            if any(marker in body.lower() for marker in _BREAKING_MARKERS)
            else Severity.UNKNOWN
        )
        return FeedPoll(
            changed=True,
            new_token=newest,
            payload_sha256=digest,
            events=[
                ChangeEvent(
                    provider_id=self.provider_id,
                    feed_key=self.feed_key,
                    source_kind=self.kind,
                    source_url=self.url,
                    old_token=state.last_token,
                    new_token=newest,
                    title=f"{self.provider_id} API version {newest}",
                    summary=f"New dated API version: {newest}",
                    body=body,
                    severity=severity,
                )
            ],
        )
