"""Unit tests for the npm registry client.

All tests use respx to intercept httpx traffic — no live network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from depfix.registry.client import (
    NpmRegistryClient,
    PackageNotFoundError,
    RegistryUnavailableError,
)

_BASE = "https://registry.example/"


def _client() -> NpmRegistryClient:
    """Build a client pointing at the mock registry."""
    return NpmRegistryClient(base_url=_BASE, timeout=1.0)


@respx.mock(base_url=_BASE)
def test_get_package_metadata_returns_parsed_response(respx_mock: respx.Router) -> None:
    respx_mock.get("/lodash").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "lodash",
                "dist-tags": {"latest": "4.17.21"},
                "versions": {"4.17.20": {}, "4.17.21": {}},
            },
        )
    )

    with _client() as c:
        meta = c.get_package_metadata("lodash")

    assert meta.name == "lodash"
    assert meta.latest == "4.17.21"
    assert set(meta.versions) == {"4.17.20", "4.17.21"}


@respx.mock(base_url=_BASE)
def test_scoped_package_is_url_encoded(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/%40types/node").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "@types/node",
                "dist-tags": {"latest": "20.11.0"},
                "versions": {"20.11.0": {}},
            },
        )
    )

    with _client() as c:
        meta = c.get_package_metadata("@types/node")

    assert route.called
    assert meta.latest == "20.11.0"


@respx.mock(base_url=_BASE)
def test_404_raises_package_not_found(respx_mock: respx.Router) -> None:
    respx_mock.get("/no-such-pkg").mock(return_value=httpx.Response(404))

    with _client() as c, pytest.raises(PackageNotFoundError):
        c.get_package_metadata("no-such-pkg")


@respx.mock(base_url=_BASE)
def test_500_raises_unavailable(respx_mock: respx.Router) -> None:
    respx_mock.get("/lodash").mock(return_value=httpx.Response(500))

    with _client() as c, pytest.raises(RegistryUnavailableError):
        c.get_package_metadata("lodash")


@respx.mock(base_url=_BASE)
def test_network_error_raises_unavailable(respx_mock: respx.Router) -> None:
    respx_mock.get("/lodash").mock(side_effect=httpx.ConnectError("boom"))

    with _client() as c, pytest.raises(RegistryUnavailableError):
        c.get_package_metadata("lodash")


@respx.mock(base_url=_BASE)
def test_abbreviated_accept_header_is_sent(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/lodash").mock(
        return_value=httpx.Response(
            200,
            json={"name": "lodash", "dist-tags": {"latest": "5.0.0"}, "versions": {"5.0.0": {}}},
        )
    )

    with _client() as c:
        c.get_package_metadata("lodash")

    request = route.calls.last.request
    assert "application/vnd.npm.install-v1+json" in request.headers["Accept"]
