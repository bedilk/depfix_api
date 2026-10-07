"""npm dist-tag feed: polls the npm registry for a package's dist-tag version."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from depfix.registry.client import (
    NpmRegistryClient,
    PackageNotFoundError,
    RegistryUnavailableError,
)
from depfix.sources.base import ChangeSource
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
)
from depfix.sources.release_notes import fetch_release_notes_for_tag
from depfix.sources.semver import is_major_bump


class NpmDistTagSource(ChangeSource):
    kind: ClassVar[SourceKind] = SourceKind.NPM_DIST_TAG

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any],
        client: NpmRegistryClient,
        http: httpx.Client | None = None,
        github_api_url: str = "",
    ) -> None:
        super().__init__(provider_id, config)
        self.package: str = self._require("package")
        self.dist_tag: str = str(config.get("dist_tag") or "latest")
        # Optional cross-reference to the package's GitHub repo (e.g.
        # "openai/openai-node") -- when set, `poll()` joins the npm
        # dist-tag move against that repo's GitHub releases to attach real
        # notes to `body` instead of leaving it empty.
        self.github_repo: str | None = config.get("github_repo")
        self._client = client
        self._http = http
        self._github_api_url = github_api_url

    @property
    def feed_key(self) -> str:
        return f"npm:{self.package}:{self.dist_tag}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            metadata = self._client.get_package_metadata(self.package)
        except PackageNotFoundError:
            return FeedPoll.missing(f"npm package not found: {self.package}")
        except RegistryUnavailableError as exc:
            return FeedPoll.failure(str(exc))

        version = metadata.dist_tags.get(self.dist_tag)
        if not version:
            return FeedPoll.missing(f"{self.package} has no '{self.dist_tag}' dist-tag")
        if version == state.last_token:
            return FeedPoll.unchanged(token=version)
        if state.last_token is None:
            return FeedPoll.baseline(version)

        severity = (
            Severity.BREAKING if is_major_bump(state.last_token, version) else Severity.UNKNOWN
        )
        body = ""
        body_url = None
        if self.github_repo and self._http is not None:
            notes = fetch_release_notes_for_tag(
                self._http, self._github_api_url, self.github_repo, version
            )
            if notes is not None:
                body = notes.body
                body_url = notes.html_url
        event = ChangeEvent(
            provider_id=self.provider_id,
            feed_key=self.feed_key,
            source_kind=self.kind,
            source_url=f"https://www.npmjs.com/package/{self.package}/v/{version}",
            old_token=state.last_token,
            new_token=version,
            title=f"{self.package} {state.last_token} → {version}",
            summary=f"npm dist-tag '{self.dist_tag}' moved",
            body=body,
            body_url=body_url,
            severity=severity,
            raw={"package": self.package, "dist_tag": self.dist_tag},
        )
        return FeedPoll(changed=True, new_token=version, events=[event])
