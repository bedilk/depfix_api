"""How old is a published version?

The cooldown gate (see depfix.orchestrator.policy) refuses to migrate a repo
onto a version that was published only hours ago -- a supply-chain safety
measure, because a version's first hours are exactly when a compromised or
yanked release is most likely still live. This module answers the one
question that gate needs: "when was version X of package P published?"

Deliberately best-effort and never-raising: if the publish time can't be
determined (network error, unusual registry, missing timestamp), the gate
treats the version as *old enough* rather than blocking a legitimate fix on
a lookup failure. Security-advisory-driven changes bypass the cooldown
entirely -- a known vulnerability outranks the freshness risk.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError):
        return None


def npm_version_published_at(
    http: httpx.Client, registry_url: str, package: str, version: str
) -> datetime | None:
    """Publish time of one npm version, from the registry's ``time`` map."""
    from urllib.parse import quote

    url = f"{registry_url.rstrip('/')}/{quote(package, safe='@')}"
    try:
        response = http.get(url)
        response.raise_for_status()
        times = response.json().get("time", {})
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return _parse_iso(times.get(version))


def pypi_version_published_at(
    http: httpx.Client, registry_url: str, package: str, version: str
) -> datetime | None:
    """Publish time of one PyPI version, from the release file upload times."""
    from urllib.parse import quote

    url = (
        f"{registry_url.rstrip('/')}/pypi/{quote(package, safe='')}/{quote(version, safe='')}/json"
    )
    try:
        response = http.get(url)
        response.raise_for_status()
        files = response.json().get("urls", [])
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    times = [_parse_iso(f.get("upload_time_iso_8601")) for f in files]
    times = [t for t in times if t is not None]
    return min(times) if times else None  # type: ignore[type-var]


def version_published_at(
    http: httpx.Client,
    *,
    ecosystem: str,
    package: str,
    version: str,
    npm_registry_url: str,
    pypi_registry_url: str,
) -> datetime | None:
    """Dispatch to the right registry, or ``None`` for an unsupported one."""
    if not package or not version:
        return None
    if ecosystem == "npm":
        return npm_version_published_at(http, npm_registry_url, package, version)
    if ecosystem in ("pypi", "pip"):
        return pypi_version_published_at(http, pypi_registry_url, package, version)
    return None


def version_age_hours(published_at: datetime | None) -> float | None:
    """Hours since ``published_at``, or ``None`` if unknown."""
    if published_at is None:
        return None
    return (datetime.now(UTC) - published_at).total_seconds() / 3600.0
