"""CHANGELOG.md feed.

Many SDKs keep a maintained ``CHANGELOG.md`` and cut no GitHub releases at
all, which left them with a registry feed as their only signal -- a version
number and nothing about what moved. This source turns the newest version
heading into a token and that section's prose into a ``body``, which is
exactly the input :class:`depfix.classify.notes.NotesClassifier` and
:func:`depfix.classify.usage_candidates.discover_usage_candidates` need.

The token is the newest *version heading*, not a content hash: a maintainer
fixing a typo in an old entry must not look like a new release.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, ClassVar

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
from depfix.sources.semver import is_major_bump, parse_semver

#: ``## 4.0.0`` / ``## [4.0.0] - 2024-01-01`` / ``# v4.0.0 (2024-01-01)``.
_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,3})\s*\[?v?(?P<version>\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?)\]?",
    re.MULTILINE,
)

_BREAKING_MARKERS = (
    "breaking change",
    "breaking changes",
    "backwards incompatible",
    "backward incompatible",
    "migration guide",
    "removed",
)


class ChangelogFileSource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.CHANGELOG_FILE

    def __init__(self, provider_id: str, config: dict[str, Any], http: httpx.Client) -> None:
        super().__init__(provider_id, config)
        url = config.get("url")
        if url:
            self.url = str(url)
        else:
            repo = self._require("repo")
            ref = str(config.get("ref") or "HEAD")
            path = str(config.get("path") or "CHANGELOG.md")
            self.url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
        self._http = http

    @property
    def feed_key(self) -> str:
        return f"changelog_file:{self.url}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            with self._http.stream("GET", self.url) as response:
                if response.status_code == 404:
                    return FeedPoll.missing(f"changelog file 404: {self.url}")
                if response.status_code >= 400:
                    return FeedPoll.failure(f"HTTP {response.status_code} for {self.url}")
                raw = read_capped(response)
                text = raw.decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            return FeedPoll.failure(f"fetch failed: {exc}")

        digest = hashlib.sha256(raw).hexdigest()
        headings = list(_HEADING_RE.finditer(text))
        if not headings:
            return FeedPoll.baseline(state.last_token or f"sha-{digest[:12]}", sha=digest)

        newest = max(
            headings,
            key=lambda m: parse_semver(m.group("version")) or (0, 0, 0),
        )
        version = newest.group("version")

        if state.last_token is None:
            return FeedPoll.baseline(version, sha=digest)
        if version == state.last_token:
            return FeedPoll.unchanged(token=version, sha=digest)

        body = _section(text, newest)
        return FeedPoll(
            changed=True,
            new_token=version,
            payload_sha256=digest,
            events=[
                ChangeEvent(
                    provider_id=self.provider_id,
                    feed_key=self.feed_key,
                    source_kind=self.kind,
                    source_url=self.url,
                    old_token=state.last_token,
                    new_token=version,
                    title=f"{self.provider_id} changelog {version}",
                    summary=f"new changelog entry: {version}",
                    body=body,
                    body_url=self.url,
                    severity=_severity(state.last_token, version, body),
                )
            ],
        )


def _section(text: str, heading: re.Match[str]) -> str:
    """Text from ``heading`` up to the next heading of the same or higher level."""
    level = len(heading.group("hashes"))
    for following in _HEADING_RE.finditer(text, heading.end()):
        if len(following.group("hashes")) <= level:
            return text[heading.start() : following.start()].strip()
    return text[heading.start() :].strip()


def _severity(old: str, new: str, body: str) -> Severity:
    if is_major_bump(old, new):
        return Severity.BREAKING
    lowered = body.lower()
    if any(marker in lowered for marker in _BREAKING_MARKERS):
        return Severity.POTENTIALLY_BREAKING
    return Severity.UNKNOWN


__all__ = ["ChangelogFileSource"]
