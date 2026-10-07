"""Shared HTTP client construction.

One place to set the User-Agent and attach a GitHub token. Unauthenticated
GitHub is 60 req/hr, which one provider with two GitHub feeds exhausts in a
day of cron ticks; authenticated is 5000.
"""

from __future__ import annotations

import httpx

from depfix.config import get_settings

USER_AGENT = "depfix/0.3 (+https://github.com/bedilk/dependency_check)"

#: Cap on how much of any prose feed we'll buffer. A feed URL is
#: admin-trusted config, but that config can point at a host we don't
#: control, and without a cap a large or slow-drip response is read to
#: completion.
MAX_FEED_BYTES = 2_000_000


def read_capped(response: httpx.Response, max_bytes: int = MAX_FEED_BYTES) -> bytes:
    """Read at most ``max_bytes`` from an open streaming response."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


def build_http_client(timeout: float | None = None) -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        timeout=timeout if timeout is not None else settings.http_timeout,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        follow_redirects=True,
    )


def github_headers() -> dict[str, str]:
    settings = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers
