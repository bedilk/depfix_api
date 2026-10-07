"""HTTP client for the public npm registry.

Read-only client used by :class:`depfix.sources.npm_dist_tag.NpmDistTagSource`.
Deliberately minimal:

* **Synchronous httpx.** One-shot polling of ≤hundreds of packages does not
  benefit from async.
* **Abbreviated response.** We send
  ``Accept: application/vnd.npm.install-v1+json`` — the registry then omits
  the huge ``readme`` blob for every version.
* **No retries.** Feeds run from cron, and cron is the retry loop. Transient
  failures bubble up as :class:`RegistryUnavailableError` for the caller to
  record.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from depfix.config import get_settings
from depfix.registry.models import PackageMetadata

logger = logging.getLogger(__name__)

_ABBREVIATED_ACCEPT = "application/vnd.npm.install-v1+json"


class RegistryError(Exception):
    """Base class for registry-related errors."""


class PackageNotFoundError(RegistryError):
    """The registry returned 404 for the requested package."""


class RegistryUnavailableError(RegistryError):
    """Network failure or non-2xx response from the registry."""


class NpmRegistryClient:
    """Thin wrapper around ``registry.npmjs.org``."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.npm_registry_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.npm_request_timeout
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=self._timeout,
            headers={
                "Accept": _ABBREVIATED_ACCEPT,
                "User-Agent": "depfix/0.2 (+https://github.com/bedilk/dependency_check)",
            },
        )

    # -- Context-manager sugar --------------------------------------------------

    def __enter__(self) -> NpmRegistryClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- Public API -------------------------------------------------------------

    def get_package_metadata(self, name: str) -> PackageMetadata:
        """Fetch metadata for ``name`` from the registry.

        Raises:
            PackageNotFoundError: on 404.
            RegistryUnavailableError: on network failure or any other non-2xx.
        """
        # Scoped packages contain a ``/`` in the URL path; quote defensively.
        url = f"{self._base_url}/{quote(name, safe='@')}"

        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise RegistryUnavailableError(str(exc)) from exc

        if response.status_code == 404:
            raise PackageNotFoundError(name)
        if response.status_code >= 400:
            raise RegistryUnavailableError(f"registry returned {response.status_code} for {name}")

        payload = response.json()
        # The abbreviated ``versions`` field is a dict keyed by version string;
        # flatten to a list for our model.
        raw_versions = payload.get("versions", {})
        if isinstance(raw_versions, dict):
            payload = {**payload, "versions": list(raw_versions.keys())}

        return PackageMetadata.model_validate(payload)
