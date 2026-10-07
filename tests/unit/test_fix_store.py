"""Unit tests for :mod:`depfix.storage.fix_store`.

Uses an in-memory SQLite database monkeypatched into ``storage.db``,
following the same pattern as ``test_repo_scan.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.apply.models import EditVerdict, FileEdit
from depfix.core.models import BreakingChange, ChangeKind, ClassificationSource
from depfix.core.pipeline import FixPipelineResult
from depfix.gh.models import PullRequest
from depfix.storage import db as db_module
from depfix.storage.fix_store import latest_fix_run, record_fix_run, record_pull_request
from depfix.storage.schema import Base, PullRequestRow, RepoRow
from depfix.verify.models import TestCase, TestFramework, TestRunResult, TestStatus
from depfix.verify.verifier import VerificationReport


@pytest.fixture
def db_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_session_factory", factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _breaking_change(**overrides: object) -> BreakingChange:
    defaults: dict[str, object] = {
        "package": "openai",
        "old_version": "3.x",
        "new_version": "4.x",
        "old_api": "openai.createModeration",
        "new_api": "openai.moderations.create",
        "description": "moderations moved under a namespace",
        "migration_guide": "use openai.moderations.create",
        "kind": ChangeKind.METHOD_RENAMED,
        "source": ClassificationSource.MANUAL,
    }
    defaults.update(overrides)
    return BreakingChange(**defaults)  # type: ignore[arg-type]


def _edit(relpath: str, verdict: EditVerdict, **overrides: object) -> FileEdit:
    defaults: dict[str, object] = {
        "relpath": relpath,
        "original_content": "old\n",
        "fixed_content": "new\n",
        "diff": "--- a\n+++ b\n",
        "verdict": verdict,
        "confidence": 0.9,
        "usages_fixed": 1,
    }
    defaults.update(overrides)
    return FileEdit(**defaults)  # type: ignore[arg-type]


def _result(**overrides: object) -> FixPipelineResult:
    defaults: dict[str, object] = {
        "repo_full_name": "acme/widgets",
        "breaking_change": _breaking_change(),
        "files_scanned": 5,
        "files_affected": 1,
        "edits": (_edit("src/chat.js", EditVerdict.KEPT),),
        "total_usages_fixed": 1,
        "total_cost": 0.01,
        "total_tokens": 123,
        "duration_ms": 456,
        "verification": None,
    }
    defaults.update(overrides)
    return FixPipelineResult(**defaults)  # type: ignore[arg-type]


# -- record_fix_run: repo + fix_run fields ---------------------------------------


def test_record_fix_run_creates_repo_and_persists_fix_run_fields(db_session: Session) -> None:
    run = record_fix_run(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=_result()
    )
    db_session.commit()

    assert db_session.get(RepoRow, "acme/widgets") is not None
    assert run.package == "openai"
    assert run.old_api == "openai.createModeration"
    assert run.new_api == "openai.moderations.create"
    assert run.kind == ChangeKind.METHOD_RENAMED.value
    assert run.files_scanned == 5
    assert run.files_affected == 1
    assert run.total_usages_fixed == 1
    assert run.total_cost == 0.01
    assert run.total_tokens == 123
    assert run.duration_ms == 456
    assert run.verified is False
    assert run.verification_skipped_reason == ""


def test_record_fix_run_reuses_existing_repo_row_across_two_runs(db_session: Session) -> None:
    record_fix_run(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=_result()
    )
    db_session.commit()
    record_fix_run(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=_result()
    )
    db_session.commit()

    repo = db_session.get(RepoRow, "acme/widgets")
    assert len(repo.fix_runs) == 2


# -- record_fix_run: every edit is stored, not just on-disk ones -----------------


def test_record_fix_run_persists_every_edit_regardless_of_verdict(db_session: Session) -> None:
    edits = (
        _edit("a.js", EditVerdict.KEPT),
        _edit("b.js", EditVerdict.REVERTED),
        _edit("c.js", EditVerdict.SKIPPED),
    )
    run = record_fix_run(
        db_session,
        repo_full_name="acme/widgets",
        owner="acme",
        name="widgets",
        result=_result(edits=edits, files_affected=3),
    )
    db_session.commit()

    verdicts = {ff.relpath: ff.verdict for ff in run.file_fixes}
    assert verdicts == {"a.js": "kept", "b.js": "reverted", "c.js": "skipped"}


# -- record_fix_run: verification test runs ---------------------------------------


def _test_run_result(
    framework: TestFramework, cases: list[TestCase], **kwargs: object
) -> TestRunResult:
    return TestRunResult(framework=framework, cases=cases, **kwargs)  # type: ignore[arg-type]


def test_record_fix_run_persists_baseline_and_after_fix_when_verification_ran(
    db_session: Session,
) -> None:
    baseline = _test_run_result(
        TestFramework.NODE_TEST,
        [TestCase(name="a", file="a.test.js", status=TestStatus.PASSED)],
        exit_code=0,
        duration_ms=10.0,
    )
    after_fix = _test_run_result(
        TestFramework.NODE_TEST,
        [
            TestCase(name="a", file="a.test.js", status=TestStatus.PASSED),
            TestCase(name="b", file="a.test.js", status=TestStatus.FAILED),
        ],
        exit_code=1,
        duration_ms=12.0,
    )
    verification = VerificationReport(ran=True, baseline=baseline, after_fix=after_fix)

    run = record_fix_run(
        db_session,
        repo_full_name="acme/widgets",
        owner="acme",
        name="widgets",
        result=_result(verification=verification),
    )
    db_session.commit()

    assert run.verified is True
    assert run.verification_skipped_reason == ""
    by_phase = {tr.phase: tr for tr in run.test_runs}
    assert set(by_phase) == {"baseline", "after_fix"}
    assert by_phase["baseline"].passed_count == 1
    assert by_phase["baseline"].failed_count == 0
    assert by_phase["after_fix"].failed_count == 1
    assert by_phase["after_fix"].failed_identities == ["a.test.js::b"]
    assert by_phase["after_fix"].framework == "node_test"


def test_record_fix_run_not_verified_and_no_test_runs_when_verification_skipped(
    db_session: Session,
) -> None:
    verification = VerificationReport(ran=False, skipped_reason="repo has no test script")

    run = record_fix_run(
        db_session,
        repo_full_name="acme/widgets",
        owner="acme",
        name="widgets",
        result=_result(verification=verification),
    )
    db_session.commit()

    assert run.verified is False
    assert run.verification_skipped_reason == "repo has no test script"
    assert run.test_runs == []


# -- latest_fix_run ----------------------------------------------------------------


def test_latest_fix_run_returns_none_for_unknown_repo(db_session: Session) -> None:
    assert latest_fix_run(db_session, "acme/never-scanned") is None


def test_latest_fix_run_returns_the_most_recent_of_several_runs(db_session: Session) -> None:
    first = record_fix_run(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=_result()
    )
    first.started_at = datetime(2024, 1, 1, tzinfo=UTC)
    db_session.commit()

    second = record_fix_run(
        db_session,
        repo_full_name="acme/widgets",
        owner="acme",
        name="widgets",
        result=_result(total_usages_fixed=99),
    )
    second.started_at = datetime(2024, 1, 2, tzinfo=UTC)
    db_session.commit()

    latest = latest_fix_run(db_session, "acme/widgets")

    assert latest is not None
    assert latest.total_usages_fixed == 99


# -- record_pull_request -----------------------------------------------------------


def test_record_pull_request_persists_fields_and_links_to_fix_run(db_session: Session) -> None:
    run = record_fix_run(
        db_session, repo_full_name="acme/widgets", owner="acme", name="widgets", result=_result()
    )
    db_session.commit()

    pr = PullRequest(
        number=42,
        html_url="https://github.example/acme/widgets/pull/42",
        head_branch="depfix/abc123",
        base_branch="main",
        already_existed=False,
    )
    row = record_pull_request(db_session, run, pr)
    db_session.commit()

    assert isinstance(row, PullRequestRow)
    assert row.fix_run_id == run.id
    assert row.number == 42
    assert row.html_url == "https://github.example/acme/widgets/pull/42"
    assert row.head_branch == "depfix/abc123"
    assert row.base_branch == "main"
    assert row.already_existed is False
    assert run.pull_request is row
