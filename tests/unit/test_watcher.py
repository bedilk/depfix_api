"""Unit tests for the Watcher orchestration loop.

Uses an in-memory SQLite database (monkeypatched into ``storage.db``) and a
scripted ``ChangeSource`` stub, so no network or real filesystem is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from depfix.providers.models import FeedSpec, ProviderSpec
from depfix.sources.base import ChangeSource, UnsupportedSourceKind
from depfix.sources.factory import SourceDeps
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
    SpecChange,
    SpecChangeKind,
)
from depfix.storage import db as db_module
from depfix.storage.schema import Base, ChangeEventRow, FeedState, Provider, SpecChangeRow
from depfix.watcher.runner import Watcher


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_session_factory", factory)


class _ScriptedSource(ChangeSource):
    kind = SourceKind.NPM_DIST_TAG

    def __init__(self, polls: list[FeedPoll], feed_key: str = "scripted") -> None:
        super().__init__("scripted-provider", {})
        self._feed_key = feed_key
        self._polls = list(polls)
        self.seen_states: list[FeedStateSnapshot] = []

    @property
    def feed_key(self) -> str:
        return self._feed_key

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        self.seen_states.append(state)
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]


class _RaisingSource(ChangeSource):
    kind = SourceKind.NPM_DIST_TAG

    def __init__(self, feed_key: str = "scripted") -> None:
        super().__init__("scripted-provider", {})
        self._feed_key = feed_key

    @property
    def feed_key(self) -> str:
        return self._feed_key

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        raise RuntimeError("kaboom")


def _fake_deps() -> SourceDeps:
    return SourceDeps(
        http=object(),  # type: ignore[arg-type]
        npm=object(),  # type: ignore[arg-type]
        pypi=object(),  # type: ignore[arg-type]
        rubygems=object(),  # type: ignore[arg-type]
        spec_cache_dir=Path("/tmp/depfix-test-cache"),
        max_spec_bytes=1024,
        github_api_url="https://example.invalid",
    )


def _watcher(
    sources: dict[tuple[str, str], ChangeSource],
    *,
    dry_run: bool = False,
    ttl_seconds: int = 0,
) -> Watcher:
    def builder(provider_id: str, feed: FeedSpec, deps: SourceDeps) -> ChangeSource:
        key = feed.config.get("feed_key", "scripted")
        try:
            return sources[(provider_id, key)]
        except KeyError:
            raise UnsupportedSourceKind(f"no stub registered for {(provider_id, key)}") from None

    # Existing tests poll twice and expect two real polls, so caching is off by default.
    return Watcher(
        deps=_fake_deps(), source_builder=builder, dry_run=dry_run, ttl_seconds=ttl_seconds
    )


def _provider(
    provider_id: str = "acme", *, feed_key: str = "scripted", enabled: bool = True
) -> ProviderSpec:
    return ProviderSpec(
        id=provider_id,
        name=provider_id.title(),
        feeds=(FeedSpec(kind=SourceKind.NPM_DIST_TAG, config={"feed_key": feed_key}),),
        enabled=enabled,
    )


def _event(
    provider_id: str = "acme",
    feed_key: str = "scripted",
    *,
    old: str | None = "1.0.0",
    new: str = "2.0.0",
    severity: Severity = Severity.BREAKING,
    spec_changes: list[SpecChange] | None = None,
) -> ChangeEvent:
    return ChangeEvent(
        provider_id=provider_id,
        feed_key=feed_key,
        source_kind=SourceKind.NPM_DIST_TAG,
        old_token=old,
        new_token=new,
        title=f"{provider_id} {new}",
        summary="scripted event",
        severity=severity,
        spec_changes=spec_changes or [],
    )


def test_baseline_persists_cursor_without_emitting_event(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.baseline("1.0.0")])
    watcher = _watcher({(provider.id, "scripted"): source})

    outcome = watcher.run_once([provider])

    assert outcome.events == []
    (report,) = outcome.reports
    assert report.status == "baseline"

    with db_module.session_scope() as session:
        state = session.scalar(select(FeedState).where(FeedState.provider_id == provider.id))
        assert state is not None
        assert state.last_token == "1.0.0"


def test_change_is_persisted_with_spec_changes(in_memory_db: Any) -> None:
    provider = _provider()
    change = SpecChange(
        kind=SpecChangeKind.PROPERTY_REMOVED,
        severity=Severity.BREAKING,
        subject="POST /v1/charges",
        pointer="paths./v1/charges.post.requestBody.amount",
        direction="request",
    )
    event = _event(provider.id, spec_changes=[change])
    source = _ScriptedSource([FeedPoll(changed=True, new_token="2.0.0", events=[event])])
    watcher = _watcher({(provider.id, "scripted"): source})

    outcome = watcher.run_once([provider])

    assert len(outcome.events) == 1
    (report,) = outcome.reports
    assert report.status == "changed"
    assert report.event_count == 1

    with db_module.session_scope() as session:
        (row,) = session.scalars(select(ChangeEventRow)).all()
        assert row.new_token == "2.0.0"
        spec_rows = session.scalars(
            select(SpecChangeRow).where(SpecChangeRow.change_event_id == row.id)
        ).all()
        assert len(spec_rows) == 1


def test_state_snapshot_is_passed_to_source_on_second_run(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.baseline("1.0.0"), FeedPoll.unchanged("1.0.0")])
    watcher = _watcher({(provider.id, "scripted"): source})

    watcher.run_once([provider])
    watcher.run_once([provider])

    assert source.seen_states[0].last_token is None
    assert source.seen_states[1].last_token == "1.0.0"


def test_duplicate_event_is_not_reinserted(in_memory_db: Any) -> None:
    provider = _provider()
    event = _event(provider.id)
    source = _ScriptedSource(
        [
            FeedPoll(changed=True, new_token="2.0.0", events=[event]),
            FeedPoll(changed=True, new_token="2.0.0", events=[event]),
        ]
    )
    watcher = _watcher({(provider.id, "scripted"): source})

    watcher.run_once([provider])
    second = watcher.run_once([provider])

    assert second.duplicates == 1
    with db_module.session_scope() as session:
        rows = session.scalars(select(ChangeEventRow)).all()
        assert len(rows) == 1


def test_failed_feed_records_error_and_does_not_advance_cursor(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.failure("boom")])
    watcher = _watcher({(provider.id, "scripted"): source})

    outcome = watcher.run_once([provider])

    (report,) = outcome.reports
    assert report.status == "failed"
    assert report.detail == "boom"

    with db_module.session_scope() as session:
        state = session.scalar(select(FeedState).where(FeedState.provider_id == provider.id))
        assert state is not None
        assert state.last_token is None
        assert state.last_error == "boom"


def test_raising_source_is_isolated(in_memory_db: Any) -> None:
    provider_a = _provider("acme")
    provider_b = _provider("globex")
    raising = _RaisingSource()
    healthy = _ScriptedSource([FeedPoll.baseline("1.0.0")])
    watcher = _watcher(
        {
            (provider_a.id, "scripted"): raising,
            (provider_b.id, "scripted"): healthy,
        }
    )

    outcome = watcher.run_once([provider_a, provider_b])

    statuses = {(r.provider_id, r.status) for r in outcome.reports}
    assert (provider_a.id, "failed") in statuses
    assert (provider_b.id, "baseline") in statuses


def test_unsupported_kind_is_reported_not_fatal(in_memory_db: Any) -> None:
    provider_a = _provider("acme")
    provider_b = _provider("globex")
    healthy = _ScriptedSource([FeedPoll.baseline("1.0.0")])
    # Only register a stub for provider_b — provider_a's feed resolves to
    # UnsupportedSourceKind via the builder's KeyError fallback.
    watcher = _watcher({(provider_b.id, "scripted"): healthy})

    outcome = watcher.run_once([provider_a, provider_b])

    statuses = {(r.provider_id, r.status) for r in outcome.reports}
    assert (provider_a.id, "unsupported") in statuses
    assert (provider_b.id, "baseline") in statuses


def test_disabled_provider_is_skipped(in_memory_db: Any) -> None:
    provider = _provider(enabled=False)

    def builder(provider_id: str, feed: FeedSpec, deps: SourceDeps) -> ChangeSource:
        raise AssertionError("source builder must not be called for a disabled provider")

    watcher = Watcher(deps=_fake_deps(), source_builder=builder)

    outcome = watcher.run_once([provider])

    (report,) = outcome.reports
    assert report.status == "skipped"

    with db_module.session_scope() as session:
        assert session.get(Provider, provider.id) is None


def test_dry_run_persists_nothing(in_memory_db: Any) -> None:
    provider = _provider()
    event = _event(provider.id)
    source = _ScriptedSource([FeedPoll(changed=True, new_token="2.0.0", events=[event])])
    watcher = _watcher({(provider.id, "scripted"): source}, dry_run=True)

    outcome = watcher.run_once([provider])

    assert len(outcome.events) == 1
    with db_module.session_scope() as session:
        assert session.get(Provider, provider.id) is None
        assert session.scalars(select(FeedState)).first() is None
        assert session.scalars(select(ChangeEventRow)).first() is None


# -- TTL cache ---------------------------------------------------------------


def test_recent_successful_poll_is_served_from_cache(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.baseline("1.0.0"), FeedPoll.unchanged("1.0.0")])
    watcher = _watcher({(provider.id, "scripted"): source}, ttl_seconds=3600)

    watcher.run_once([provider])
    second = watcher.run_once([provider])

    assert len(source.seen_states) == 1  # no second HTTP poll
    (report,) = second.reports
    assert report.status == "cached"
    assert "1.0.0" in report.detail
    assert second.feeds_polled == 0 and second.feeds_cached == 1
    assert not second.all_failed


def test_failed_poll_is_never_cached(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.failure("boom"), FeedPoll.baseline("1.0.0")])
    watcher = _watcher({(provider.id, "scripted"): source}, ttl_seconds=3600)

    watcher.run_once([provider])
    second = watcher.run_once([provider])

    assert len(source.seen_states) == 2
    assert second.reports[0].status == "baseline"


def test_expired_ttl_polls_again(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.baseline("1.0.0"), FeedPoll.unchanged("1.0.0")])
    watcher = _watcher({(provider.id, "scripted"): source}, ttl_seconds=3600)
    watcher.run_once([provider])

    with db_module.session_scope() as session:
        state = session.scalar(select(FeedState))
        state.last_polled_at = datetime.now(UTC) - timedelta(hours=2)

    second = watcher.run_once([provider])

    assert len(source.seen_states) == 2
    assert second.reports[0].status == "unchanged"


def test_future_last_polled_at_does_not_freeze_the_feed(in_memory_db: Any) -> None:
    provider = _provider()
    source = _ScriptedSource([FeedPoll.baseline("1.0.0"), FeedPoll.unchanged("1.0.0")])
    watcher = _watcher({(provider.id, "scripted"): source}, ttl_seconds=3600)
    watcher.run_once([provider])

    with db_module.session_scope() as session:
        session.scalar(select(FeedState)).last_polled_at = datetime.now(UTC) + timedelta(days=1)

    watcher.run_once([provider])
    assert len(source.seen_states) == 2


def test_one_failure_among_cached_feeds_is_not_all_failed(in_memory_db: Any) -> None:
    healthy, flaky = _provider("acme"), _provider("globex")
    ok = _ScriptedSource([FeedPoll.baseline("1.0.0")])
    bad = _ScriptedSource([FeedPoll.failure("boom")])
    watcher = _watcher(
        {(healthy.id, "scripted"): ok, (flaky.id, "scripted"): bad}, ttl_seconds=3600
    )

    watcher.run_once([healthy, flaky])
    second = watcher.run_once([healthy, flaky])

    assert second.feeds_cached == 1 and len(second.failures) == 1
    assert not second.all_failed  # cron must not page
