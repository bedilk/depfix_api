"""Unit tests for :mod:`depfix.storage.attempt_store` -- the fleet
orchestrator's idempotency ledger.

Uses an in-memory SQLite database, following the same pattern as
``test_fix_store.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.storage.attempt_store import (
    get_change_attempt,
    record_change_attempt,
    touch_no_call_sites,
)
from depfix.storage.schema import AttemptStatus, Base, ChangeAttemptRow, RepoRow


@pytest.fixture
def db_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(RepoRow(id="acme/widgets", owner="acme", name="widgets"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


# -- get_change_attempt -----------------------------------------------------------


def test_get_change_attempt_returns_none_when_never_attempted(db_session: Session) -> None:
    assert get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk") is None


def test_get_change_attempt_is_scoped_to_repo_and_dedupe_key(db_session: Session) -> None:
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()

    assert get_change_attempt(db_session, repo_id="acme/other", dedupe_key="dk") is None
    assert get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="other-dk") is None
    assert get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk") is not None


# -- record_change_attempt: create ------------------------------------------------


def test_record_change_attempt_creates_a_new_row(db_session: Session) -> None:
    row = record_change_attempt(
        db_session,
        repo_id="acme/widgets",
        dedupe_key="dk",
        status=AttemptStatus.PR_OPENED,
        fix_run_id=7,
        ref_sha="sha-1",
    )
    db_session.commit()

    assert row.status == AttemptStatus.PR_OPENED.value
    assert row.attempts_used == 1
    assert row.fix_run_id == 7
    assert row.last_error == ""
    assert row.last_checked_ref_sha == "sha-1"


def test_record_change_attempt_defaults_last_error_to_empty_string(db_session: Session) -> None:
    row = record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FIXED_NO_PR
    )
    assert row.last_error == ""


def test_record_change_attempt_without_ref_sha_leaves_it_empty(db_session: Session) -> None:
    row = record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()
    assert row.last_checked_ref_sha == ""


# -- record_change_attempt: idempotent update -------------------------------------


def test_record_change_attempt_is_idempotent_on_repo_and_dedupe_key(db_session: Session) -> None:
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()

    rows = db_session.query(ChangeAttemptRow).all()
    assert len(rows) == 1


def test_record_change_attempt_increments_attempts_used_on_each_call(
    db_session: Session,
) -> None:
    for _ in range(3):
        record_change_attempt(
            db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
        )
        db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.attempts_used == 3


def test_record_change_attempt_updates_status_in_place(db_session: Session) -> None:
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.PR_OPENED
    )
    db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.status == AttemptStatus.PR_OPENED.value


def test_record_change_attempt_overwrites_fix_run_id_with_none_when_omitted(
    db_session: Session,
) -> None:
    """A later attempt that doesn't pass ``fix_run_id`` (e.g. a retry that
    failed before ever reaching a fix run) must clear the previous one --
    the ledger reflects the *latest* attempt, not a sticky historical
    best."""
    record_change_attempt(
        db_session,
        repo_id="acme/widgets",
        dedupe_key="dk",
        status=AttemptStatus.FIXED_NO_PR,
        fix_run_id=7,
    )
    db_session.commit()
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.fix_run_id is None


def test_record_change_attempt_clears_last_error_on_success(db_session: Session) -> None:
    record_change_attempt(
        db_session,
        repo_id="acme/widgets",
        dedupe_key="dk",
        status=AttemptStatus.FAILED,
        last_error="boom",
    )
    db_session.commit()
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.PR_OPENED
    )
    db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.last_error == ""


def test_record_change_attempt_updates_ref_sha_only_when_given(db_session: Session) -> None:
    record_change_attempt(
        db_session,
        repo_id="acme/widgets",
        dedupe_key="dk",
        status=AttemptStatus.NO_CALL_SITES,
        ref_sha="sha-1",
    )
    db_session.commit()
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.last_checked_ref_sha == "sha-1"


def test_two_repos_can_each_have_their_own_attempt_for_the_same_change(
    db_session: Session,
) -> None:
    db_session.add(RepoRow(id="acme/other", owner="acme", name="other"))
    db_session.commit()

    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.PR_OPENED
    )
    record_change_attempt(
        db_session, repo_id="acme/other", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()

    widgets = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    other = get_change_attempt(db_session, repo_id="acme/other", dedupe_key="dk")
    assert widgets is not None and widgets.status == AttemptStatus.PR_OPENED.value
    assert other is not None and other.status == AttemptStatus.FAILED.value


# -- touch_no_call_sites -----------------------------------------------------------


def test_touch_no_call_sites_creates_a_row_without_consuming_attempt_budget(
    db_session: Session,
) -> None:
    row = touch_no_call_sites(db_session, repo_id="acme/widgets", dedupe_key="dk", ref_sha="sha-1")
    db_session.commit()

    assert row.status == AttemptStatus.NO_CALL_SITES.value
    assert row.last_checked_ref_sha == "sha-1"
    assert row.attempts_used == 0


def test_touch_no_call_sites_refreshes_ref_sha_on_an_existing_row(db_session: Session) -> None:
    touch_no_call_sites(db_session, repo_id="acme/widgets", dedupe_key="dk", ref_sha="sha-1")
    db_session.commit()
    touch_no_call_sites(db_session, repo_id="acme/widgets", dedupe_key="dk", ref_sha="sha-2")
    db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.last_checked_ref_sha == "sha-2"
    assert row.attempts_used == 0


def test_touch_no_call_sites_does_not_touch_attempts_used_of_a_prior_failure(
    db_session: Session,
) -> None:
    """A change that previously failed for real (consuming attempt budget)
    and is now found to have no call sites at all must not have that
    failure's ``attempts_used`` silently reset -- ``touch_no_call_sites``
    only ever changes ``status``/``last_checked_ref_sha``."""
    record_change_attempt(
        db_session, repo_id="acme/widgets", dedupe_key="dk", status=AttemptStatus.FAILED
    )
    db_session.commit()
    touch_no_call_sites(db_session, repo_id="acme/widgets", dedupe_key="dk", ref_sha="sha-1")
    db_session.commit()

    row = get_change_attempt(db_session, repo_id="acme/widgets", dedupe_key="dk")
    assert row is not None
    assert row.status == AttemptStatus.NO_CALL_SITES.value
    assert row.attempts_used == 1
