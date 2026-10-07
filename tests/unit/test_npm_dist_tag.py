"""Unit tests for the npm dist-tag feed, including its optional join against
GitHub release notes (``github_repo`` config key).
"""

from __future__ import annotations

import httpx
import respx

from depfix.registry.models import PackageMetadata
from depfix.sources.models import FeedStateSnapshot
from depfix.sources.npm_dist_tag import NpmDistTagSource

_GH_BASE = "https://api.github.example"


class _FakeRegistryClient:
    def __init__(self, dist_tags: dict[str, str]) -> None:
        self._metadata = PackageMetadata(name="widgets", dist_tags=dist_tags, versions=[])

    def get_package_metadata(self, name: str) -> PackageMetadata:
        return self._metadata


def test_poll_leaves_body_empty_without_github_repo_configured() -> None:
    source = NpmDistTagSource(
        "acme", {"package": "widgets"}, _FakeRegistryClient({"latest": "2.0.0"})
    )

    result = source.poll(FeedStateSnapshot(last_token="1.0.0"))

    assert len(result.events) == 1
    event = result.events[0]
    assert event.body == ""
    assert event.body_url is None


def test_poll_leaves_body_empty_when_no_http_client_configured() -> None:
    source = NpmDistTagSource(
        "acme",
        {"package": "widgets", "github_repo": "acme/widgets"},
        _FakeRegistryClient({"latest": "2.0.0"}),
    )

    result = source.poll(FeedStateSnapshot(last_token="1.0.0"))

    assert result.events[0].body == ""


@respx.mock(base_url=_GH_BASE)
def test_poll_joins_release_notes_when_github_repo_configured(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/2.0.0").mock(
        return_value=httpx.Response(
            200, json={"body": "breaking changes here", "html_url": "https://example/2.0.0"}
        )
    )

    source = NpmDistTagSource(
        "acme",
        {"package": "widgets", "github_repo": "acme/widgets"},
        _FakeRegistryClient({"latest": "2.0.0"}),
        http=httpx.Client(base_url=_GH_BASE),
        github_api_url=_GH_BASE,
    )

    result = source.poll(FeedStateSnapshot(last_token="1.0.0"))

    event = result.events[0]
    assert event.body == "breaking changes here"
    assert event.body_url == "https://example/2.0.0"


@respx.mock(base_url=_GH_BASE)
def test_poll_leaves_body_empty_when_join_finds_no_release(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/2.0.0").mock(return_value=httpx.Response(404))
    respx_mock.get("/repos/acme/widgets/releases/tags/v2.0.0").mock(
        return_value=httpx.Response(404)
    )

    source = NpmDistTagSource(
        "acme",
        {"package": "widgets", "github_repo": "acme/widgets"},
        _FakeRegistryClient({"latest": "2.0.0"}),
        http=httpx.Client(base_url=_GH_BASE),
        github_api_url=_GH_BASE,
    )

    result = source.poll(FeedStateSnapshot(last_token="1.0.0"))

    event = result.events[0]
    assert event.body == ""
    assert event.body_url is None
