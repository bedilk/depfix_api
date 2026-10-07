"""GitHub Releases feed. Token is the newest release's tag name."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from depfix.sources.base import ChangeSource
from depfix.sources.http import github_headers
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
)
from depfix.sources.semver import is_major_bump

_BREAKING_MARKERS = (
    "breaking change",
    "breaking changes",
    "backwards incompatible",
    "backward incompatible",
    "migration guide",
    "!:",
    "removed support",
)


class GitHubReleaseSource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.GITHUB_RELEASE

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any],
        http: httpx.Client,
        api_url: str,
    ) -> None:
        super().__init__(provider_id, config)
        self.repo: str = self._require("repo")
        self.include_prereleases: bool = bool(config.get("include_prereleases", False))
        self._http = http
        self._api_url = api_url.rstrip("/")

    @property
    def feed_key(self) -> str:
        return f"github_release:{self.repo}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        url = f"{self._api_url}/repos/{self.repo}/releases"
        try:
            response = self._http.get(url, params={"per_page": 20}, headers=github_headers())
        except httpx.HTTPError as exc:
            return FeedPoll.failure(f"fetch failed: {exc}")

        if response.status_code == 404:
            return FeedPoll.missing(f"no releases endpoint for {self.repo}")
        if response.status_code == 403:
            return FeedPoll.failure(
                "GitHub rate limit or forbidden — set GITHUB_TOKEN (60 → 5000 req/hr)"
            )
        if response.status_code >= 400:
            return FeedPoll.failure(f"HTTP {response.status_code} for {url}")

        payload = response.json()
        if not isinstance(payload, list):
            return FeedPoll.failure("unexpected releases payload shape")

        release = self._pick(payload)
        if release is None:
            return FeedPoll.missing(f"{self.repo} has no published releases")

        tag = str(release.get("tag_name") or "")
        if not tag:
            return FeedPoll.failure("newest release has no tag_name")
        if tag == state.last_token:
            return FeedPoll.unchanged(token=tag)

        if state.last_token is None:
            return FeedPoll.baseline(tag)

        body = str(release.get("body") or "")
        event = ChangeEvent(
            provider_id=self.provider_id,
            feed_key=self.feed_key,
            source_kind=self.kind,
            source_url=str(release.get("html_url") or ""),
            old_token=state.last_token,
            new_token=tag,
            title=str(release.get("name") or tag),
            summary=f"{self.repo} released {tag}",
            body=body,
            severity=self._severity(state.last_token, tag, body),
            raw={
                "published_at": release.get("published_at"),
                "prerelease": release.get("prerelease"),
            },
        )
        return FeedPoll(changed=True, new_token=tag, events=[event])

    def _pick(self, releases: list[Any]) -> dict[str, Any] | None:
        for entry in releases:
            if not isinstance(entry, dict) or entry.get("draft"):
                continue
            if entry.get("prerelease") and not self.include_prereleases:
                continue
            return entry
        return None

    def _severity(self, old_tag: str, new_tag: str, body: str) -> Severity:
        if is_major_bump(old_tag, new_tag):
            return Severity.BREAKING
        lowered = body.lower()
        if any(marker in lowered for marker in _BREAKING_MARKERS):
            return Severity.POTENTIALLY_BREAKING
        return Severity.UNKNOWN
