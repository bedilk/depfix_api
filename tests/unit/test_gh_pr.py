"""Unit tests for :mod:`depfix.gh.pr`.

All GitHub HTTP traffic is intercepted with respx -- no live network, same
style as ``test_gh_branch.py``.
"""

from __future__ import annotations

import httpx
import respx

from depfix.apply.models import EditVerdict, FileEdit
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.core.pipeline import FixPipelineResult
from depfix.gh.pr import build_pr_body, kept_edits, open_pull_request, pr_title_for
from depfix.verify.verifier import VerificationReport

_BASE = "https://api.github.example"


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
        source_url="https://example.com/changelog",
        evidence="createModeration was removed in v4",
    )


def _edit(relpath: str, verdict: EditVerdict, usages_fixed: int = 1) -> FileEdit:
    return FileEdit(
        relpath=relpath,
        original_content="old\n",
        fixed_content="new\n",
        diff="",
        verdict=verdict,
        usages_fixed=usages_fixed,
    )


def _result(edits: list[FileEdit], verification: VerificationReport | None) -> FixPipelineResult:
    return FixPipelineResult(
        repo_full_name="acme/widgets",
        breaking_change=_breaking_change(),
        files_scanned=len(edits),
        files_affected=len(edits),
        edits=tuple(edits),
        total_usages_fixed=sum(e.usages_fixed for e in edits),
        total_cost=0.0,
        total_tokens=0,
        duration_ms=1,
        verification=verification,
    )


def test_pr_title_for_uses_old_and_new_api() -> None:
    title = pr_title_for(_breaking_change())
    assert "openai.createModeration" in title
    assert "openai.moderations.create" in title


def test_kept_edits_filters_to_kept_only() -> None:
    edits = [
        _edit("src/a.js", EditVerdict.KEPT),
        _edit("src/b.js", EditVerdict.SUSPECT),
        _edit("src/c.js", EditVerdict.REVERTED),
    ]
    result = _result(edits, VerificationReport(ran=True))
    assert [e.relpath for e in kept_edits(result)] == ["src/a.js"]


def test_build_pr_body_includes_evidence_and_file_table() -> None:
    edits = [_edit("src/chat.js", EditVerdict.KEPT, usages_fixed=2)]
    verification = VerificationReport(ran=True)
    result = _result(edits, verification)

    body = build_pr_body(_breaking_change(), result)

    assert "openai.createModeration" in body
    assert "use openai.moderations.create" in body
    assert "createModeration was removed in v4" in body
    assert "https://example.com/changelog" in body
    assert "src/chat.js" in body
    assert "kept" in body


def test_build_pr_body_notes_when_verification_did_not_run() -> None:
    edits = [_edit("src/chat.js", EditVerdict.SUSPECT)]
    verification = VerificationReport(ran=False, skipped_reason="no test script")
    result = _result(edits, verification)

    body = build_pr_body(_breaking_change(), result)

    assert "no test script" in body


@respx.mock(base_url=_BASE)
def test_open_pull_request_creates_new_pr(respx_mock: respx.Router) -> None:
    route = respx_mock.post("/repos/acme/widgets/pulls").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 42,
                "html_url": "https://github.example/acme/widgets/pull/42",
                "head": {"ref": "depfix/abc"},
                "base": {"ref": "main"},
            },
        )
    )

    pr = open_pull_request(
        owner="acme",
        repo="widgets",
        token="ghs_token",
        head_branch="depfix/abc",
        base_branch="main",
        title="depfix: migrate x -> y",
        body="body",
        api_url=_BASE,
    )

    assert pr.number == 42
    assert pr.html_url == "https://github.example/acme/widgets/pull/42"
    assert pr.head_branch == "depfix/abc"
    assert pr.base_branch == "main"
    assert pr.already_existed is False

    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    assert sent["head"] == "depfix/abc"
    assert sent["base"] == "main"
    assert sent["draft"] is False


@respx.mock(base_url=_BASE)
def test_open_pull_request_returns_existing_pr_on_422(respx_mock: respx.Router) -> None:
    respx_mock.post("/repos/acme/widgets/pulls").mock(
        return_value=httpx.Response(422, text="A pull request already exists")
    )
    list_route = respx_mock.get("/repos/acme/widgets/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "html_url": "https://github.example/acme/widgets/pull/7",
                    "head": {"ref": "depfix/abc"},
                    "base": {"ref": "main"},
                }
            ],
        )
    )

    pr = open_pull_request(
        owner="acme",
        repo="widgets",
        token="ghs_token",
        head_branch="depfix/abc",
        base_branch="main",
        title="depfix: migrate x -> y",
        body="body",
        api_url=_BASE,
    )

    assert pr.number == 7
    assert pr.already_existed is True
    assert list_route.called
    assert list_route.calls.last.request.url.params["head"] == "acme:depfix/abc"
