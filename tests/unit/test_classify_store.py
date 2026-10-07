"""Unit tests for :mod:`depfix.classify.store`'s ``classify_pending`` --
in particular, that its terminal ``unclassifiable`` outcomes are counted
separately from real errors and still get ``classified_at`` stamped.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.classify.classifier import Classifier
from depfix.classify.store import classify_pending, event_from_row
from depfix.storage.schema import Base, ChangeEventRow, Provider


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(Provider(id="acme", name="Acme"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _add_event(session: Session, *, body: str = "", body_url: str | None = None) -> ChangeEventRow:
    row = ChangeEventRow(
        provider_id="acme",
        # This helper covers the empty-release-notes path. Registry events
        # are intentionally classified as dependency drift even without a
        # prose body, so they belong in classifier-specific tests instead.
        feed_key="github_release:acme/widgets",
        source_kind="github_release",
        old_token="1.0.0",
        new_token="2.0.0",
        body=body,
        body_url=body_url,
        severity="unknown",
        dedupe_key=f"dk-{body}-{body_url}",
        detected_at=datetime.now(tz=UTC),
    )
    session.add(row)
    session.commit()
    return row


def test_classify_pending_counts_unclassifiable_events(db_session: Session) -> None:
    _add_event(db_session, body="")
    classifier = Classifier(None)

    summary = classify_pending(db_session, classifier)

    assert summary.events_processed == 1
    assert summary.unclassifiable == 1
    assert summary.changes_created == 0
    assert summary.errors == []


def test_classify_pending_stamps_classified_at_for_unclassifiable_events(
    db_session: Session,
) -> None:
    row = _add_event(db_session, body="")
    classifier = Classifier(None)

    classify_pending(db_session, classifier)

    assert row.classified_at is not None


def test_classify_pending_does_not_stamp_classified_at_on_real_errors(db_session: Session) -> None:
    row = _add_event(db_session, body="release notes with no LLM configured")
    classifier = Classifier(None)

    summary = classify_pending(db_session, classifier)

    assert summary.unclassifiable == 0
    assert len(summary.errors) == 1
    assert row.classified_at is None


def test_event_from_row_round_trips_body_url(db_session: Session) -> None:
    row = _add_event(db_session, body="notes", body_url="https://example/releases/2.0.0")

    event = event_from_row(row)

    assert event.body_url == "https://example/releases/2.0.0"
