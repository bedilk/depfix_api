"""Pushes a batch of confirmed-clean :class:`~depfix.apply.models.FileEdit`\\ s
to a new branch via GitHub's Git Data API (blobs -> tree -> commit -> ref).

Deliberately takes a plain bearer ``token: str`` rather than a
:class:`depfix.gh.app.GitHubAppAuth` -- same pattern as
:meth:`depfix.clone.service.CloneService.clone`, which also just wants a
token string and shouldn't need to know how it was minted.
"""

from __future__ import annotations

import base64
import logging

import httpx

from depfix.apply.models import FileEdit
from depfix.core.models import BreakingChange
from depfix.gh.app import GitHubAppError

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_API_URL = "https://api.github.com"

#: Every branch depfix creates or fast-forwards starts with this prefix --
#: the one place that literal is defined, so the orchestrator's
#: open-PR-count check (:meth:`depfix.orchestrator.runner.Orchestrator._run_repo`)
#: and :func:`branch_name_for` can never drift apart the way they did before
#: (one hardcoded ``"depfix/"`` here, another hardcoded in the orchestrator).
BRANCH_PREFIX = "depfix/"


def branch_name_for_key(key: str) -> str:
    """Deterministic branch for any depfix work unit identified by ``key``."""
    return f"{BRANCH_PREFIX}{key[:12]}"


def branch_name_for(change: BreakingChange) -> str:
    """Deterministic branch name for ``change``, so repeated runs for the
    same breaking change target the same branch instead of piling up a new
    one every time."""
    return branch_name_for_key(change.dedupe_key)


class BranchWriter:
    """Commits a batch of edits to a new (or existing, idempotently
    updated) branch, entirely through GitHub's REST API -- no local git
    checkout or push credentials involved.

    Not safe to share across threads or reuse across unrelated fix
    batches -- like :class:`depfix.verify.verifier.Verifier`, one instance
    is scoped to one commit_edits() call's worth of work.
    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        *,
        api_url: str = DEFAULT_GITHUB_API_URL,
        http_timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=http_timeout)
        self._owns_client = client is None
        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BranchWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def commit_edits(
        self, *, branch: str, base_branch: str, message: str, edits: list[FileEdit]
    ) -> str:
        """Commit ``edits`` on top of ``base_branch``'s current tip,
        creating ``branch`` if it doesn't exist yet or fast-forwarding it
        (idempotently) if it does. Returns the new commit sha.
        """
        base_sha = self._base_commit_sha(base_branch)
        base_tree_sha = self._tree_sha_for_commit(base_sha)

        tree_entries = [
            {
                "path": edit.relpath,
                "mode": "100644",
                "type": "blob",
                "sha": self._create_blob(edit.fixed_content),
            }
            for edit in edits
        ]
        tree_sha = self._create_tree(base_tree_sha, tree_entries)
        commit_sha = self._create_commit(message, tree_sha, parents=[base_sha])
        self._upsert_ref(branch, commit_sha)
        return commit_sha

    # -- Git Data API steps -----------------------------------------------------

    def _base_commit_sha(self, base_branch: str) -> str:
        resp = self._client.get(
            f"{self._api_url}/repos/{self._owner}/{self._repo}/git/ref/heads/{base_branch}",
            headers=self._headers,
        )
        _raise_for_status(resp)
        return resp.json()["object"]["sha"]

    def _tree_sha_for_commit(self, commit_sha: str) -> str:
        resp = self._client.get(
            f"{self._api_url}/repos/{self._owner}/{self._repo}/git/commits/{commit_sha}",
            headers=self._headers,
        )
        _raise_for_status(resp)
        return resp.json()["tree"]["sha"]

    def _create_blob(self, content: str) -> str:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        resp = self._client.post(
            f"{self._api_url}/repos/{self._owner}/{self._repo}/git/blobs",
            headers=self._headers,
            json={"content": encoded, "encoding": "base64"},
        )
        _raise_for_status(resp)
        return resp.json()["sha"]

    def _create_tree(self, base_tree_sha: str, entries: list[dict]) -> str:
        resp = self._client.post(
            f"{self._api_url}/repos/{self._owner}/{self._repo}/git/trees",
            headers=self._headers,
            json={"base_tree": base_tree_sha, "tree": entries},
        )
        _raise_for_status(resp)
        return resp.json()["sha"]

    def _create_commit(self, message: str, tree_sha: str, *, parents: list[str]) -> str:
        resp = self._client.post(
            f"{self._api_url}/repos/{self._owner}/{self._repo}/git/commits",
            headers=self._headers,
            json={"message": message, "tree": tree_sha, "parents": parents},
        )
        _raise_for_status(resp)
        return resp.json()["sha"]

    def _upsert_ref(self, branch: str, commit_sha: str) -> None:
        resp = self._client.post(
            f"{self._api_url}/repos/{self._owner}/{self._repo}/git/refs",
            headers=self._headers,
            json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
        if resp.status_code == 422:
            # Branch already exists from a previous run of this same
            # deterministic name -- fast-forward it to the new commit
            # rather than failing. `force` is safe here because this
            # branch is owned entirely by depfix (see `branch_name_for`),
            # never a branch a human is also pushing to.
            patch_resp = self._client.patch(
                f"{self._api_url}/repos/{self._owner}/{self._repo}/git/refs/heads/{branch}",
                headers=self._headers,
                json={"sha": commit_sha, "force": True},
            )
            _raise_for_status(patch_resp)
            return
        _raise_for_status(resp)


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise GitHubAppError(
            f"GitHub rejected the token (401 Unauthorized) for {resp.request.url}. "
            "The installation token may have expired or lack the required permission."
        )
    if resp.status_code == 404:
        raise GitHubAppError(f"GitHub resource not found: {resp.request.url}")
    if resp.status_code == 422:
        raise GitHubAppError(
            f"GitHub rejected the request as unprocessable (422): {resp.text[:300]}"
        )
    if resp.status_code >= 400:
        raise GitHubAppError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")
