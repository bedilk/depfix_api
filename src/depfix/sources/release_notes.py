"""Best-effort join: given an npm package's new version, look up the
matching GitHub release so ``NpmDistTagSource`` can attach real release
notes to ``body`` instead of leaving it empty.

Deliberately never raises and never turns into a ``FeedPoll.failure`` — the
primary feed here is the npm dist-tag itself; a 404 (repo doesn't tag
releases, or tags a convention we haven't guessed) or any network error just
means ``body`` stays empty, exactly as it already was before this join
existed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from depfix.sources.http import github_headers


@dataclass(frozen=True)
class ReleaseNotes:
    body: str
    html_url: str


def fetch_release_notes_for_tag(
    http: httpx.Client, api_url: str, repo: str, version: str
) -> ReleaseNotes | None:
    """Look up the GitHub release tagged for ``version`` in ``repo``.

    Tries the bare npm version string first, then the conventional
    ``v``-prefixed GitHub tag — projects are split roughly evenly on which
    one they actually tag releases with, so both are worth a try before
    giving up.
    """
    base = api_url.rstrip("/")
    candidates = [version] if version.startswith("v") else [version, f"v{version}"]
    for tag in candidates:
        notes = _fetch_tag(http, base, repo, tag)
        if notes is not None:
            return notes
    return None


def _fetch_tag(http: httpx.Client, base: str, repo: str, tag: str) -> ReleaseNotes | None:
    url = f"{base}/repos/{repo}/releases/tags/{tag}"
    try:
        response = http.get(url, headers=github_headers())
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    body = str(payload.get("body") or "")
    if not body:
        return None
    return ReleaseNotes(body=body, html_url=str(payload.get("html_url") or ""))
