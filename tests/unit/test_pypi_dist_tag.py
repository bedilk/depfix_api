"""Unit tests for the PyPI latest-version feed."""

from __future__ import annotations

import httpx
import respx

from depfix.registry.pypi_models import PypiPackageMetadata
from depfix.sources.models import FeedStateSnapshot, Severity
from depfix.sources.pypi_dist_tag import PypiDistTagSource

_GH_BASE = "https://api.github.example"


class _FakePypiClient:
    def __init__(self, latest: str) -> None:
        self._metadata = PypiPackageMetadata(name="openai", latest=latest, versions=[])

    def get_package_metadata(self, name: str) -> PypiPackageMetadata:
        return self._metadata


def test_first_sight_is_a_baseline_and_version_movement_has_severity() -> None:
    source = PypiDistTagSource("openai", {"package": "openai"}, _FakePypiClient("1.4.3"))
    baseline = source.poll(FeedStateSnapshot(last_token=None))
    assert baseline.new_token == "1.4.3"
    assert not baseline.events

    source = PypiDistTagSource("openai", {"package": "openai"}, _FakePypiClient("2.0.0"))
    changed = source.poll(FeedStateSnapshot(last_token="1.4.3"))
    (event,) = changed.events
    assert event.severity is Severity.BREAKING
    assert event.raw["ecosystem"] == "pypi"
    assert event.feed_key == "pypi:openai"


@respx.mock(base_url=_GH_BASE)
def test_joins_github_release_notes_when_configured(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/openai/openai-python/releases/tags/2.0.0").mock(
        return_value=httpx.Response(200, json={"body": "breaking", "html_url": "u"})
    )
    source = PypiDistTagSource(
        "openai",
        {"package": "openai", "github_repo": "openai/openai-python"},
        _FakePypiClient("2.0.0"),
        http=httpx.Client(base_url=_GH_BASE),
        github_api_url=_GH_BASE,
    )

    result = source.poll(FeedStateSnapshot(last_token="1.4.3"))

    assert result.events[0].body == "breaking"
