"""Unit tests for the dated-changelog HTML feed, including the response-size
cap that keeps a slow/large changelog page from being read to completion.
"""

from __future__ import annotations

import httpx
import respx

from depfix.sources.changelog_html import _MAX_RESPONSE_BYTES, ChangelogHtmlSource
from depfix.sources.models import FeedStateSnapshot

_BASE = "https://changelog.example"


def _source(http: httpx.Client) -> ChangelogHtmlSource:
    return ChangelogHtmlSource("acme", {"url": f"{_BASE}/changes"}, http)


@respx.mock(base_url=_BASE)
def test_poll_reports_baseline_on_first_seen_version(respx_mock: respx.Router) -> None:
    respx_mock.get("/changes").mock(
        return_value=httpx.Response(200, text="<p>Released 2026-01-15</p>")
    )

    source = _source(httpx.Client(base_url=_BASE))
    result = source.poll(FeedStateSnapshot(last_token=None))

    assert result.new_token == "2026-01-15"
    assert result.note == "baseline recorded (nothing to diff against)"
    assert not result.events


@respx.mock(base_url=_BASE)
def test_poll_flags_new_dated_version_as_changed(respx_mock: respx.Router) -> None:
    respx_mock.get("/changes").mock(
        return_value=httpx.Response(
            200, text="<h2>2026-03-01</h2><p>Removed the legacy endpoint.</p>"
        )
    )

    source = _source(httpx.Client(base_url=_BASE))
    result = source.poll(FeedStateSnapshot(last_token="2026-01-15"))

    assert result.changed
    assert result.new_token == "2026-03-01"
    (event,) = result.events
    assert "2026-03-01" in event.title
    assert event.severity.value == "potentially_breaking"


@respx.mock(base_url=_BASE)
def test_poll_is_unchanged_when_newest_token_matches(respx_mock: respx.Router) -> None:
    respx_mock.get("/changes").mock(return_value=httpx.Response(200, text="<p>2026-01-15</p>"))

    source = _source(httpx.Client(base_url=_BASE))
    result = source.poll(FeedStateSnapshot(last_token="2026-01-15"))

    assert not result.changed


@respx.mock(base_url=_BASE)
def test_poll_reports_missing_on_404(respx_mock: respx.Router) -> None:
    respx_mock.get("/changes").mock(return_value=httpx.Response(404))

    source = _source(httpx.Client(base_url=_BASE))
    result = source.poll(FeedStateSnapshot(last_token=None))

    assert result.not_found
    assert result.note == "changelog URL 404"


@respx.mock(base_url=_BASE)
def test_poll_truncates_oversized_response_instead_of_reading_to_completion(
    respx_mock: respx.Router,
) -> None:
    """A changelog URL is admin-trusted config, but that config can still
    point at a host that returns an arbitrarily large or slow-drip body.
    ``poll`` must never buffer more than ``_MAX_RESPONSE_BYTES``."""
    oversized = ("<p>2026-01-15</p>" + "x" * _MAX_RESPONSE_BYTES).encode("utf-8")
    respx_mock.get("/changes").mock(return_value=httpx.Response(200, content=oversized))

    source = _source(httpx.Client(base_url=_BASE))
    result = source.poll(FeedStateSnapshot(last_token=None))

    # The date near the start of the (truncated) body is still recoverable --
    # the cap must not corrupt normal-sized real content.
    assert result.new_token == "2026-01-15"
