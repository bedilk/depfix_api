"""PyPI latest-version feed, parallel to the npm dist-tag source."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from depfix.registry.pypi_client import (
    PypiPackageNotFoundError,
    PypiRegistryClient,
    PypiUnavailableError,
)
from depfix.sources.base import ChangeSource
from depfix.sources.models import ChangeEvent, FeedPoll, FeedStateSnapshot, Severity, SourceKind
from depfix.sources.release_notes import fetch_release_notes_for_tag
from depfix.sources.semver import is_major_bump


class PypiDistTagSource(ChangeSource):
    """Poll PyPI's ``info.version`` latest pointer for one SDK package."""

    kind: ClassVar[SourceKind] = SourceKind.PYPI_DIST_TAG

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any],
        client: PypiRegistryClient,
        http: httpx.Client | None = None,
        github_api_url: str = "",
    ) -> None:
        super().__init__(provider_id, config)
        self.package: str = self._require("package")
        self.github_repo: str | None = config.get("github_repo")
        self._client = client
        self._http = http
        self._github_api_url = github_api_url

    @property
    def feed_key(self) -> str:
        return f"pypi:{self.package}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        try:
            metadata = self._client.get_package_metadata(self.package)
        except PypiPackageNotFoundError:
            return FeedPoll.missing(f"PyPI project not found: {self.package}")
        except PypiUnavailableError as exc:
            return FeedPoll.failure(str(exc))
        version = metadata.latest
        if not version:
            return FeedPoll.missing(f"{self.package} has no published version on PyPI")
        if version == state.last_token:
            return FeedPoll.unchanged(token=version)
        if state.last_token is None:
            return FeedPoll.baseline(version)
        body = ""
        body_url = None
        if self.github_repo and self._http is not None:
            notes = fetch_release_notes_for_tag(
                self._http, self._github_api_url, self.github_repo, version
            )
            if notes is not None:
                body, body_url = notes.body, notes.html_url
        event = ChangeEvent(
            provider_id=self.provider_id,
            feed_key=self.feed_key,
            source_kind=self.kind,
            source_url=f"https://pypi.org/project/{self.package}/{version}/",
            old_token=state.last_token,
            new_token=version,
            title=f"{self.package} {state.last_token} -> {version}",
            summary="PyPI latest version moved",
            body=body,
            body_url=body_url,
            severity=Severity.BREAKING
            if is_major_bump(state.last_token, version)
            else Severity.UNKNOWN,
            raw={"package": self.package, "ecosystem": "pypi"},
        )
        return FeedPoll(changed=True, new_token=version, events=[event])
