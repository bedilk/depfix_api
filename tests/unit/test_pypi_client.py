"""Unit tests for the PyPI registry client."""

from __future__ import annotations

import httpx
import pytest
import respx

from depfix.registry.pypi_client import (
    PypiPackageNotFoundError,
    PypiRegistryClient,
    PypiUnavailableError,
)

_BASE = "https://pypi.example"


def _client() -> PypiRegistryClient:
    return PypiRegistryClient(base_url=_BASE, timeout=1.0)


@respx.mock(base_url=_BASE)
def test_get_package_metadata_parses_info_and_releases(respx_mock: respx.Router) -> None:
    respx_mock.get("/pypi/openai/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "info": {"name": "openai", "version": "1.4.3"},
                "releases": {"1.4.2": [], "1.4.3": []},
            },
        )
    )

    with _client() as client:
        metadata = client.get_package_metadata("openai")

    assert metadata.name == "openai"
    assert metadata.latest == "1.4.3"
    assert set(metadata.versions) == {"1.4.2", "1.4.3"}


@respx.mock(base_url=_BASE)
def test_404_raises_not_found(respx_mock: respx.Router) -> None:
    respx_mock.get("/pypi/nope/json").mock(return_value=httpx.Response(404))

    with _client() as client, pytest.raises(PypiPackageNotFoundError):
        client.get_package_metadata("nope")


@respx.mock(base_url=_BASE)
def test_500_and_network_errors_raise_unavailable(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/pypi/openai/json")
    route.mock(return_value=httpx.Response(500))
    with _client() as client, pytest.raises(PypiUnavailableError):
        client.get_package_metadata("openai")

    route.mock(side_effect=httpx.ConnectError("boom"))
    with _client() as client, pytest.raises(PypiUnavailableError):
        client.get_package_metadata("openai")
