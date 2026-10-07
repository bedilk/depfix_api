"""Read-only client for the RubyGems API.

Mirrors :mod:`depfix.registry.pypi_client`: one endpoint, one question --
what is this gem's latest version. Without it, a ``rubygems`` entry in
``providers.yaml`` would be configured and permanently dark, because
nothing would ever produce a feed version to compare a Gemfile against.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from depfix.config import get_settings

DEFAULT_RUBYGEMS_URL = "https://rubygems.org"


class RubyGemsError(Exception):
    """Base class for RubyGems registry failures."""


class GemNotFoundError(RubyGemsError):
    """The requested gem does not exist."""


class RubyGemsUnavailableError(RubyGemsError):
    """RubyGems could not be reached or returned an error response."""


class RubyGemsClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.rubygems_registry_url).rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout if timeout is not None else settings.rubygems_request_timeout,
            headers={"Accept": "application/json", "User-Agent": "depfix/0.2"},
        )

    def __enter__(self) -> RubyGemsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def latest_version(self, name: str) -> str:
        url = f"{self._base_url}/api/v1/versions/{quote(name, safe='')}/latest.json"
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise RubyGemsUnavailableError(str(exc)) from exc
        if response.status_code == 404:
            raise GemNotFoundError(name)
        if response.status_code >= 400:
            raise RubyGemsUnavailableError(f"RubyGems returned {response.status_code} for {name}")
        try:
            version = str((response.json() or {}).get("version") or "")
        except ValueError as exc:
            raise RubyGemsUnavailableError(f"RubyGems returned invalid JSON for {name}") from exc
        if not version or version == "unknown":
            raise GemNotFoundError(name)
        return version
