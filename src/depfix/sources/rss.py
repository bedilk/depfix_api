"""RSS 2.0 / Atom feed.

Some vendors announce API changes on a blog feed and nowhere machine-readable
-- no spec, no GitHub release. The newest entry's stable id is the token and
its content is the ``body``, so the same prose path applies.

Parsed with the stdlib XML parser already used elsewhere in the codebase.
Both dialects are handled because vendors are split between them and the
difference is two element names.
"""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar

import defusedxml.ElementTree as ElementTree
import httpx

from depfix.sources.base import ChangeSource
from depfix.sources.http import read_capped
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
)

_ATOM = "{http://www.w3.org/2005/Atom}"

_BREAKING_MARKERS = (
    "breaking change",
    "breaking changes",
    "deprecat",
    "removed",
    "migration",
    "sunset",
    "end of life",
)


class RssSource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.RSS

    def __init__(self, provider_id: str, config: dict[str, Any], http: httpx.Client) -> None:
        super().__init__(provider_id, config)
        self.url = self._require("url")
        self._http = http

    @property
    def feed_key(self) -> str:
        return f"rss:{self.url}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            with self._http.stream("GET", self.url) as response:
                if response.status_code == 404:
                    return FeedPoll.missing(f"RSS URL 404: {self.url}")
                if response.status_code >= 400:
                    return FeedPoll.failure(f"HTTP {response.status_code} for {self.url}")
                raw = read_capped(response)
        except httpx.HTTPError as exc:
            return FeedPoll.failure(f"fetch failed: {exc}")

        digest = hashlib.sha256(raw).hexdigest()
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            return FeedPoll.failure(f"feed is not parseable XML: {exc}")

        entry = _newest_entry(root)
        if entry is None:
            return FeedPoll.missing(f"{self.url} has no entries")

        token, title, body, link = entry
        if state.last_token is None:
            return FeedPoll.baseline(token, sha=digest)
        if token == state.last_token:
            return FeedPoll.unchanged(token=token, sha=digest)

        lowered = f"{title}\n{body}".lower()
        severity = (
            Severity.POTENTIALLY_BREAKING
            if any(marker in lowered for marker in _BREAKING_MARKERS)
            else Severity.UNKNOWN
        )
        return FeedPoll(
            changed=True,
            new_token=token,
            payload_sha256=digest,
            events=[
                ChangeEvent(
                    provider_id=self.provider_id,
                    feed_key=self.feed_key,
                    source_kind=self.kind,
                    source_url=link or self.url,
                    old_token=state.last_token,
                    new_token=token,
                    title=title or token,
                    summary=f"new feed entry: {title or token}",
                    body=body,
                    body_url=link or self.url,
                    severity=severity,
                )
            ],
        )


def _newest_entry(root: ElementTree.Element) -> tuple[str, str, str, str] | None:
    """``(token, title, body, link)`` for the first entry, or ``None``.

    Feed order is the publisher's problem: both dialects put newest first
    by convention, and there is no reliable cross-vendor date format to
    sort on.
    """
    item = root.find(".//item")
    if item is not None:
        token = _text(item, "guid") or _text(item, "link") or _text(item, "title") or ""
        body = _text(item, "description") or _text(
            item, "{http://purl.org/rss/1.0/modules/content/}encoded"
        )
        return (token, _text(item, "title"), body, _text(item, "link")) if token else None

    entry = root.find(f".//{_ATOM}entry")
    if entry is None:
        return None
    link_el = entry.find(f"{_ATOM}link")
    link = link_el.get("href", "") if link_el is not None else ""
    token = _text(entry, f"{_ATOM}id") or link or _text(entry, f"{_ATOM}title")
    body = _text(entry, f"{_ATOM}content") or _text(entry, f"{_ATOM}summary")
    return (token, _text(entry, f"{_ATOM}title"), body, link) if token else None


def _text(parent: ElementTree.Element, tag: str) -> str:
    found = parent.find(tag)
    return (found.text or "").strip() if found is not None else ""


__all__ = ["RssSource"]
