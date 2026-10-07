"""Unit tests for the npm↔GitHub release-notes join.

All tests use respx to intercept httpx traffic — no live network.
"""

from __future__ import annotations

import httpx
import respx

from depfix.sources.release_notes import fetch_release_notes_for_tag

_BASE = "https://api.github.example"


@respx.mock(base_url=_BASE)
def test_fetch_release_notes_finds_bare_version_tag(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/1.2.3").mock(
        return_value=httpx.Response(
            200, json={"body": "release notes", "html_url": "https://example/releases/1.2.3"}
        )
    )

    notes = fetch_release_notes_for_tag(
        httpx.Client(base_url=_BASE), _BASE, "acme/widgets", "1.2.3"
    )

    assert notes is not None
    assert notes.body == "release notes"
    assert notes.html_url == "https://example/releases/1.2.3"


@respx.mock(base_url=_BASE)
def test_fetch_release_notes_falls_back_to_v_prefixed_tag(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/1.2.3").mock(return_value=httpx.Response(404))
    respx_mock.get("/repos/acme/widgets/releases/tags/v1.2.3").mock(
        return_value=httpx.Response(200, json={"body": "v-prefixed notes", "html_url": "u"})
    )

    notes = fetch_release_notes_for_tag(
        httpx.Client(base_url=_BASE), _BASE, "acme/widgets", "1.2.3"
    )

    assert notes is not None
    assert notes.body == "v-prefixed notes"


@respx.mock(base_url=_BASE)
def test_fetch_release_notes_returns_none_when_neither_tag_exists(
    respx_mock: respx.Router,
) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/1.2.3").mock(return_value=httpx.Response(404))
    respx_mock.get("/repos/acme/widgets/releases/tags/v1.2.3").mock(
        return_value=httpx.Response(404)
    )

    notes = fetch_release_notes_for_tag(
        httpx.Client(base_url=_BASE), _BASE, "acme/widgets", "1.2.3"
    )

    assert notes is None


@respx.mock(base_url=_BASE)
def test_fetch_release_notes_returns_none_on_network_error(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/1.2.3").mock(
        side_effect=httpx.ConnectError("boom")
    )
    respx_mock.get("/repos/acme/widgets/releases/tags/v1.2.3").mock(
        side_effect=httpx.ConnectError("boom")
    )

    notes = fetch_release_notes_for_tag(
        httpx.Client(base_url=_BASE), _BASE, "acme/widgets", "1.2.3"
    )

    assert notes is None


@respx.mock(base_url=_BASE)
def test_fetch_release_notes_returns_none_for_empty_body(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/releases/tags/1.2.3").mock(
        return_value=httpx.Response(200, json={"body": "", "html_url": "u"})
    )
    respx_mock.get("/repos/acme/widgets/releases/tags/v1.2.3").mock(
        return_value=httpx.Response(404)
    )

    notes = fetch_release_notes_for_tag(
        httpx.Client(base_url=_BASE), _BASE, "acme/widgets", "1.2.3"
    )

    assert notes is None


@respx.mock(base_url=_BASE)
def test_fetch_release_notes_does_not_retry_bare_tag_when_already_v_prefixed(
    respx_mock: respx.Router,
) -> None:
    route = respx_mock.get("/repos/acme/widgets/releases/tags/v1.2.3").mock(
        return_value=httpx.Response(200, json={"body": "notes", "html_url": "u"})
    )

    notes = fetch_release_notes_for_tag(
        httpx.Client(base_url=_BASE), _BASE, "acme/widgets", "v1.2.3"
    )

    assert notes is not None
    assert route.call_count == 1
