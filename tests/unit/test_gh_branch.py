"""Unit tests for :mod:`depfix.gh.branch`.

All GitHub HTTP traffic is intercepted with respx -- no live network, same
style as ``test_gh_app.py``. ``BranchWriter`` takes a plain bearer token
string, so unlike ``test_gh_app.py`` there's no RSA key/JWT involved here.
"""

from __future__ import annotations

import httpx
import respx

from depfix.apply.models import EditVerdict, FileEdit
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.gh.branch import BranchWriter, branch_name_for

_BASE = "https://api.github.example"


def _edit(relpath: str, fixed_content: str) -> FileEdit:
    return FileEdit(
        relpath=relpath,
        original_content="old\n",
        fixed_content=fixed_content,
        diff="",
        verdict=EditVerdict.KEPT,
    )


def _breaking_change() -> BreakingChange:
    return BreakingChange(
        package="openai",
        old_version="3.x",
        new_version="4.x",
        old_api="openai.createModeration",
        new_api="openai.moderations.create",
        description="moderations moved under a namespace",
        migration_guide="use openai.moderations.create",
        kind=ChangeKind.METHOD_RENAMED,
        source=ClassificationSource.MANUAL,
        provider_id="openai",
    )


def _writer() -> BranchWriter:
    return BranchWriter("ghs_token", "acme", "widgets", api_url=_BASE)


def test_branch_name_for_is_deterministic_and_repeatable() -> None:
    change = _breaking_change()
    assert branch_name_for(change) == branch_name_for(change)
    assert branch_name_for(change).startswith("depfix/")


@respx.mock(base_url=_BASE)
def test_commit_edits_creates_blobs_tree_commit_and_ref(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "base-commit-sha"}})
    )
    respx_mock.get("/repos/acme/widgets/git/commits/base-commit-sha").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "base-tree-sha"}})
    )
    blob_route = respx_mock.post("/repos/acme/widgets/git/blobs").mock(
        return_value=httpx.Response(200, json={"sha": "blob-sha"})
    )
    tree_route = respx_mock.post("/repos/acme/widgets/git/trees").mock(
        return_value=httpx.Response(200, json={"sha": "new-tree-sha"})
    )
    commit_route = respx_mock.post("/repos/acme/widgets/git/commits").mock(
        return_value=httpx.Response(200, json={"sha": "new-commit-sha"})
    )
    ref_route = respx_mock.post("/repos/acme/widgets/git/refs").mock(
        return_value=httpx.Response(201, json={"ref": "refs/heads/depfix/abc"})
    )

    with _writer() as writer:
        sha = writer.commit_edits(
            branch="depfix/abc",
            base_branch="main",
            message="depfix: migrate openai v3 -> v4",
            edits=[_edit("src/chat.js", "new content\n")],
        )

    assert sha == "new-commit-sha"

    import json as _json

    blob_body = _json.loads(blob_route.calls.last.request.content)
    assert blob_body["encoding"] == "base64"

    tree_body = _json.loads(tree_route.calls.last.request.content)
    assert tree_body["base_tree"] == "base-tree-sha"
    assert tree_body["tree"] == [
        {"path": "src/chat.js", "mode": "100644", "type": "blob", "sha": "blob-sha"}
    ]

    commit_body = _json.loads(commit_route.calls.last.request.content)
    assert commit_body["tree"] == "new-tree-sha"
    assert commit_body["parents"] == ["base-commit-sha"]

    ref_body = _json.loads(ref_route.calls.last.request.content)
    assert ref_body["ref"] == "refs/heads/depfix/abc"
    assert ref_body["sha"] == "new-commit-sha"


@respx.mock(base_url=_BASE)
def test_commit_edits_patches_existing_ref_on_422(respx_mock: respx.Router) -> None:
    respx_mock.get("/repos/acme/widgets/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "base-commit-sha"}})
    )
    respx_mock.get("/repos/acme/widgets/git/commits/base-commit-sha").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "base-tree-sha"}})
    )
    respx_mock.post("/repos/acme/widgets/git/blobs").mock(
        return_value=httpx.Response(200, json={"sha": "blob-sha"})
    )
    respx_mock.post("/repos/acme/widgets/git/trees").mock(
        return_value=httpx.Response(200, json={"sha": "new-tree-sha"})
    )
    respx_mock.post("/repos/acme/widgets/git/commits").mock(
        return_value=httpx.Response(200, json={"sha": "new-commit-sha"})
    )
    respx_mock.post("/repos/acme/widgets/git/refs").mock(
        return_value=httpx.Response(422, text="Reference already exists")
    )
    patch_route = respx_mock.patch("/repos/acme/widgets/git/refs/heads/depfix/abc").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/depfix/abc"})
    )

    with _writer() as writer:
        sha = writer.commit_edits(
            branch="depfix/abc",
            base_branch="main",
            message="depfix: migrate openai v3 -> v4",
            edits=[_edit("src/chat.js", "new content\n")],
        )

    assert sha == "new-commit-sha"
    assert patch_route.called

    import json as _json

    patch_body = _json.loads(patch_route.calls.last.request.content)
    assert patch_body["sha"] == "new-commit-sha"
    assert patch_body["force"] is True
