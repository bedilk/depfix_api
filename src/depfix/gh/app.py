"""GitHub App authentication.

Two credentials, deliberately:

- **App JWT** -- RS256, signed locally with the App's private key, <=10 min
  TTL, sent only to ``/app/*`` endpoints (list installations, mint tokens).
- **Installation Access Token** -- minted via the JWT, ~1hr TTL, scoped to
  specific repositories and permissions requested at mint time.

Least privilege: :meth:`GitHubAppAuth.installation_token` always requests
``{"contents": "read"}`` and a single repository by default, regardless of
what the installation itself was granted. A token minted by this process can
read exactly the one repo it needs and nothing else, so a leaked token's
blast radius is one repo's file contents -- not "everything this App can
see."
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import jwt

from depfix.gh.models import Installation, InstallationToken, PullRequest, Repository

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_API_URL = "https://api.github.com"

# JWT TTL well under GitHub's 10-minute ceiling, with room for clock skew.
_JWT_TTL_SECONDS = 570
# Backdate `iat` so a local clock that runs a little fast doesn't produce a
# token GitHub considers "not yet valid".
_JWT_CLOCK_BACKDATE_SECONDS = 60


class GitHubAppError(RuntimeError):
    """Raised for any GitHub App auth/API failure, with an actionable message."""


class GitHubAppAuth:
    """Mints and caches installation tokens from a GitHub App's private key."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        api_url: str = DEFAULT_GITHUB_API_URL,
        http_timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=http_timeout)
        self._owns_client = client is None
        # (installation_id, repo scope, permissions) -> token. Scope and
        # permissions are both part of the key: a token minted for repo A
        # must never be handed out for repo B, and a read-only token cached
        # for a repo must never be handed out when a caller asks for
        # write access to that same repo.
        self._token_cache: dict[
            tuple[int, tuple[str, ...], tuple[tuple[str, str], ...]], InstallationToken
        ] = {}
        # (owner, repo) -> Installation. Unlike tokens, an installation
        # binding doesn't expire -- every pre-flight read in the fleet
        # orchestrator's per-repo path (get_file_contents, ref_sha,
        # list_open_pull_requests) independently re-resolved this before
        # caching landed, so one repo cost 3-4 GET /repos/{o}/{r}/installation
        # round trips before a single change was even considered.
        self._installation_cache: dict[tuple[str, str], Installation] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubAppAuth:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- App-level JWT ----------------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - _JWT_CLOCK_BACKDATE_SECONDS,
            "exp": now + _JWT_TTL_SECONDS,
            "iss": self._app_id,
        }
        try:
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except Exception as exc:
            raise GitHubAppError(
                f"failed to sign App JWT: {exc}. Check that GITHUB_APP_PRIVATE_KEY "
                "is a valid PEM-encoded RSA private key."
            ) from exc

    def _app_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # -- HTTP plumbing ------------------------------------------------------------

    def _raise_for_status(
        self, resp: httpx.Response, *, requested_permissions: dict[str, str] | None = None
    ) -> None:
        if resp.status_code == 401:
            raise GitHubAppError(
                "GitHub rejected the App JWT (401 Unauthorized). Most likely cause: "
                "local clock skew -- the JWT's iat/exp must be within a few minutes "
                "of GitHub's clock. Check `date` on this machine, and that "
                "GITHUB_APP_PRIVATE_KEY matches GITHUB_APP_ID."
            )
        if resp.status_code == 404:
            raise GitHubAppError(
                f"GitHub App resource not found: {resp.request.url}. The App may not "
                "be installed on the target account/repository."
            )
        if resp.status_code == 422:
            permission_detail = (
                ", ".join(
                    f"{name}={level}" for name, level in sorted(requested_permissions.items())
                )
                if requested_permissions
                else "the required permission"
            )
            raise GitHubAppError(
                f"GitHub rejected the request as unprocessable (422): {resp.text[:300]}. "
                f"The token request asked for: {permission_detail}. Grant those permissions "
                "to the GitHub App and approve the updated installation, or confirm the repo "
                "is covered by the installation's repository selection."
            )
        if resp.status_code >= 400:
            raise GitHubAppError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")

    def _paginate(
        self, url: str, headers: dict[str, str], *, list_key: str | None = None
    ) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        while next_url:
            resp = self._client.get(next_url, headers=headers)
            self._raise_for_status(resp)
            data = resp.json()
            page = data.get(list_key, []) if list_key else data
            items.extend(page)
            next_url = _next_link(resp.headers.get("Link"))
        return items

    # -- App-level endpoints --------------------------------------------------

    def list_installations(self) -> list[Installation]:
        raw = self._paginate(f"{self._api_url}/app/installations", self._app_headers())
        return [_installation_from_dict(item) for item in raw]

    def resolve_installation_for_repo(self, owner: str, repo: str) -> Installation:
        """Return this App's installation for ``owner/repo``, caching by
        ``(owner, repo)`` for this ``GitHubAppAuth``'s lifetime -- an
        installation binding doesn't change mid-run, so every other method
        on this class that needs it (``get_file_contents``, ``ref_sha``,
        ``list_open_pull_requests``, ...) shares one lookup per repo instead
        of re-resolving it on every call.
        """
        cache_key = (owner, repo)
        cached = self._installation_cache.get(cache_key)
        if cached is not None:
            return cached

        resp = self._client.get(
            f"{self._api_url}/repos/{owner}/{repo}/installation", headers=self._app_headers()
        )
        if resp.status_code == 404:
            raise GitHubAppError(
                f"no GitHub App installation found for {owner}/{repo}. Install the "
                "App on this repository (or its owning org) first."
            )
        self._raise_for_status(resp)
        installation = _installation_from_dict(resp.json())
        self._installation_cache[cache_key] = installation
        return installation

    # -- Installation tokens --------------------------------------------------

    def installation_token(
        self,
        installation_id: int,
        *,
        repositories: tuple[str, ...] = (),
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken:
        """Mint (or return a cached, unexpired) least-privilege token.

        Defaults to ``{"contents": "read"}`` scoped to ``repositories``,
        regardless of what the installation itself was granted.
        """
        effective_permissions = permissions or {"contents": "read"}
        permissions_key = tuple(sorted(effective_permissions.items()))
        cache_key = (installation_id, repositories, permissions_key)
        cached = self._token_cache.get(cache_key)
        if cached is not None and not cached.is_expired():
            return cached

        payload: dict[str, object] = {"permissions": effective_permissions}
        if repositories:
            payload["repositories"] = list(repositories)

        resp = self._client.post(
            f"{self._api_url}/app/installations/{installation_id}/access_tokens",
            headers=self._app_headers(),
            json=payload,
        )
        self._raise_for_status(resp, requested_permissions=effective_permissions)
        data = resp.json()
        token = InstallationToken(
            token=data["token"],
            expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).astimezone(
                UTC
            ),
            installation_id=installation_id,
            repositories=repositories,
        )
        self._token_cache[cache_key] = token
        return token

    def _installation_headers(
        self,
        installation_id: int,
        *,
        repositories: tuple[str, ...] = (),
        permissions: dict[str, str] | None = None,
    ) -> dict[str, str]:
        token = self.installation_token(
            installation_id, repositories=repositories, permissions=permissions
        )
        return {
            "Authorization": f"token {token.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # -- Repository endpoints (installation-scoped) --------------------------

    def list_repositories(self, installation_id: int) -> list[Repository]:
        headers = self._installation_headers(installation_id)
        raw = self._paginate(
            f"{self._api_url}/installation/repositories", headers, list_key="repositories"
        )
        return [_repository_from_dict(item) for item in raw]

    def get_repository(self, owner: str, repo: str) -> Repository:
        installation = self.resolve_installation_for_repo(owner, repo)
        headers = self._installation_headers(installation.id, repositories=(repo,))
        resp = self._client.get(f"{self._api_url}/repos/{owner}/{repo}", headers=headers)
        self._raise_for_status(resp)
        return _repository_from_dict(resp.json())

    def default_ref(self, owner: str, repo: str) -> str:
        return self.get_repository(owner, repo).default_branch

    # -- Pre-flight reads (fleet orchestrator) ---------------------------------
    #
    # Cheap, read-only lookups the orchestrator needs *before* deciding
    # whether a repo is worth cloning at all: does it opt in via
    # `.depfix.yml`, has its HEAD moved since the last run, and do we
    # already have an open PR for this change.

    def get_file_contents(
        self, owner: str, repo: str, path: str, *, ref: str | None = None
    ) -> str | None:
        """Decoded text contents of ``path`` at ``ref``, or ``None`` if it
        doesn't exist there. A missing file is the common case here (most
        repos haven't opted in to ``.depfix.yml``) -- not an error."""
        installation = self.resolve_installation_for_repo(owner, repo)
        headers = self._installation_headers(installation.id, repositories=(repo,))
        url = f"{self._api_url}/repos/{owner}/{repo}/contents/{path}"
        resp = self._client.get(url, headers=headers, params={"ref": ref} if ref else None)
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        data = resp.json()
        if isinstance(data, list):
            raise GitHubAppError(f"{owner}/{repo}:{path} is a directory, not a file")
        encoding = data.get("encoding", "base64")
        if encoding != "base64":
            raise GitHubAppError(
                f"{owner}/{repo}:{path}: unsupported content encoding {encoding!r}"
            )
        return base64.b64decode(data["content"]).decode("utf-8")

    def ref_sha(self, owner: str, repo: str, ref: str) -> str:
        """The commit sha ``refs/heads/{ref}`` currently points at. Lets the
        orchestrator tell whether a repo has moved since its last run --
        outcomes like ``no_call_sites`` only need rechecking once HEAD
        actually changes, not on every cron tick."""
        installation = self.resolve_installation_for_repo(owner, repo)
        headers = self._installation_headers(installation.id, repositories=(repo,))
        resp = self._client.get(
            f"{self._api_url}/repos/{owner}/{repo}/git/ref/heads/{ref}", headers=headers
        )
        self._raise_for_status(resp)
        return str(resp.json()["object"]["sha"])

    def list_open_pull_requests(
        self, owner: str, repo: str, *, head: str | None = None, base: str | None = None
    ) -> list[PullRequest]:
        """Open PRs for ``owner/repo``, optionally filtered by head/base
        branch. Lets the orchestrator check "did we already open a PR for
        this branch" with a read-only token, before ever minting one scoped
        to ``pull_requests: write``."""
        installation = self.resolve_installation_for_repo(owner, repo)
        headers = self._installation_headers(
            installation.id, repositories=(repo,), permissions={"pull_requests": "read"}
        )
        params: dict[str, str] = {"state": "open"}
        if head:
            params["head"] = f"{owner}:{head}"
        if base:
            params["base"] = base
        url = f"{self._api_url}/repos/{owner}/{repo}/pulls?{urlencode(params)}"
        raw = self._paginate(url, headers)
        return [_pull_request_from_dict(item) for item in raw]

    def get_pull_request_state(self, owner: str, repo: str, number: int) -> dict:
        """Return GitHub's current state fields for one PR."""
        installation = self.resolve_installation_for_repo(owner, repo)
        headers = self._installation_headers(
            installation.id, repositories=(repo,), permissions={"pull_requests": "read"}
        )
        response = self._client.get(
            f"{self._api_url}/repos/{owner}/{repo}/pulls/{number}", headers=headers
        )
        self._raise_for_status(response)
        payload = response.json()
        return {
            "state": str(payload.get("state") or "").lower(),
            "merged": bool(payload.get("merged")),
            "merged_at": payload.get("merged_at"),
            "closed_at": payload.get("closed_at"),
        }


def _installation_from_dict(item: dict) -> Installation:
    account = item.get("account") or {}
    return Installation(
        id=item["id"],
        account_login=account.get("login", ""),
        account_type=account.get("type", ""),
        repository_selection=item.get("repository_selection", "selected"),
    )


def _repository_from_dict(item: dict) -> Repository:
    return Repository(
        id=item["id"],
        full_name=item["full_name"],
        default_branch=item.get("default_branch", "main"),
        private=bool(item.get("private", False)),
    )


def _pull_request_from_dict(item: dict) -> PullRequest:
    return PullRequest(
        number=item["number"],
        html_url=item["html_url"],
        head_branch=item["head"]["ref"],
        base_branch=item["base"]["ref"],
        already_existed=False,
    )


def _next_link(link_header: str | None) -> str | None:
    """Parse the RFC 5988 ``Link`` header GitHub sends for pagination."""
    if not link_header:
        return None
    for part in link_header.split(","):
        pieces = part.split(";")
        if len(pieces) < 2:
            continue
        url = pieces[0].strip().lstrip("<").rstrip(">")
        rel = pieces[1].strip()
        if rel == 'rel="next"':
            return url
    return None
