"""RubyGems latest-version feed, parallel to the npm and PyPI sources."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from depfix.registry.rubygems_client import (
    GemNotFoundError,
    RubyGemsClient,
    RubyGemsUnavailableError,
)
from depfix.sources.base import ChangeSource
from depfix.sources.models import ChangeEvent, FeedPoll, FeedStateSnapshot, Severity, SourceKind
from depfix.sources.semver import is_major_bump


class RubyGemsDistTagSource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.RUBYGEMS_DIST_TAG

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any],
        client: RubyGemsClient,
        http: httpx.Client | None = None,
        github_api_url: str = "",
    ) -> None:
        super().__init__(provider_id, config)
        self.gem: str = self._require("gem")
        self.github_repo: str | None = config.get("github_repo")
        self._client = client
        self._http = http
        self._github_api_url = github_api_url

    @property
    def feed_key(self) -> str:
        return f"rubygems:{self.gem}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            version = self._client.latest_version(self.gem)
        except GemNotFoundError:
            return FeedPoll.missing(f"gem not found: {self.gem}")
        except RubyGemsUnavailableError as exc:
            return FeedPoll.failure(str(exc))
        if version == state.last_token:
            return FeedPoll.unchanged(token=version)
        if state.last_token is None:
            return FeedPoll.baseline(version)

        return FeedPoll(
            changed=True,
            new_token=version,
            events=[
                ChangeEvent(
                    provider_id=self.provider_id,
                    feed_key=self.feed_key,
                    source_kind=self.kind,
                    source_url=f"https://rubygems.org/gems/{self.gem}/versions/{version}",
                    old_token=state.last_token,
                    new_token=version,
                    title=f"{self.gem} {state.last_token} -> {version}",
                    summary="RubyGems latest version moved",
                    severity=(
                        Severity.BREAKING
                        if is_major_bump(state.last_token, version)
                        else Severity.UNKNOWN
                    ),
                    raw={"package": self.gem, "ecosystem": "rubygems"},
                )
            ],
        )
