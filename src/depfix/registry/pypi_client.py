"""Read-only client for the public PyPI JSON API."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from depfix.config import get_settings
from depfix.registry.pypi_models import PypiPackageMetadata

DEFAULT_PYPI_URL = "https://pypi.org"


class PypiRegistryError(Exception):
    """Base class for PyPI registry failures."""


class PypiPackageNotFoundError(PypiRegistryError):
    """The requested PyPI project does not exist."""


class PypiUnavailableError(PypiRegistryError):
    """PyPI could not be reached or returned an error response."""


class PypiRegistryClient:
    """Small synchronous adapter around ``/pypi/{project}/json``."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.pypi_registry_url).rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout if timeout is not None else settings.pypi_request_timeout,
            headers={"Accept": "application/json", "User-Agent": "depfix/0.2"},
        )

    def __enter__(self) -> PypiRegistryClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_package_metadata(self, name: str) -> PypiPackageMetadata:
        """Fetch current project metadata or raise a domain-specific error."""
        url = f"{self._base_url}/pypi/{quote(name, safe='')}/json"
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise PypiUnavailableError(str(exc)) from exc
        if response.status_code == 404:
            raise PypiPackageNotFoundError(name)
        if response.status_code >= 400:
            raise PypiUnavailableError(f"PyPI returned {response.status_code} for {name}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PypiUnavailableError(f"PyPI returned invalid JSON for {name}") from exc
        info = payload.get("info") or {}
        releases = payload.get("releases") or {}
        return PypiPackageMetadata(
            name=str(info.get("name") or name),
            latest=str(info.get("version") or ""),
            versions=list(releases) if isinstance(releases, dict) else [],
        )
