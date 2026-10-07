"""Feed watcher — the top-level detection loop.

Replaced the old npm-only poller as the trunk. One-shot by design: cron is
the scheduler and cron is the retry loop (unchanged from the Day 3-4 ADR).

Failure isolation: one bad feed must never lose the results of the others, so
each provider commits independently and every ``poll()`` is wrapped. A source
that raises is a bug in that source, not a reason to abort the run.

Caching: a feed whose last *successful* poll is younger than ``ttl_seconds``
is served from ``feed_state`` without any HTTP call (status ``"cached"``).
A failed poll is never cached -- the next tick retries it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from depfix.config import get_settings
from depfix.providers.models import FeedSpec, ProviderSpec
from depfix.sources.base import ChangeSource, UnsupportedSourceKind
from depfix.sources.factory import SourceDeps, build_source
from depfix.sources.models import ChangeEvent, FeedPoll, FeedStateSnapshot, Severity
from depfix.storage.db import init_schema, session_scope
from depfix.storage.schema import ChangeEventRow, FeedState, Provider, SpecChangeRow

logger = logging.getLogger(__name__)

SourceBuilder = Callable[[str, FeedSpec, SourceDeps], ChangeSource]


@dataclass
class FeedReport:
    """Per-feed line item for the CLI table."""

    provider_id: str
    feed_key: str
    kind: str
    # changed | unchanged | baseline | cached | not_found | unsupported | failed | skipped
    status: str
    detail: str = ""
    event_count: int = 0

    @property
    def is_failure(self) -> bool:
        return self.status == "failed"


@dataclass
class WatchOutcome:
    reports: list[FeedReport] = field(default_factory=list)
    events: list[ChangeEvent] = field(default_factory=list)
    duplicates: int = 0

    @property
    def feeds_polled(self) -> int:
        """Feeds that actually hit the network this run."""
        return sum(1 for r in self.reports if r.status not in ("skipped", "cached"))

    @property
    def feeds_cached(self) -> int:
        return sum(1 for r in self.reports if r.status == "cached")

    @property
    def failures(self) -> list[FeedReport]:
        return [r for r in self.reports if r.is_failure]

    @property
    def all_failed(self) -> bool:
        """True only when every enabled feed is dark. A cached feed was
        healthy on its last poll, so it counts as alive."""
        considered = self.feeds_polled + self.feeds_cached
        return bool(self.failures) and len(self.failures) == considered

    @property
    def breaking_events(self) -> list[ChangeEvent]:
        return [e for e in self.events if e.severity is Severity.BREAKING]

    @property
    def has_changes(self) -> bool:
        return bool(self.events)


class Watcher:
    def __init__(
        self,
        deps: SourceDeps,
        source_builder: SourceBuilder = build_source,
        dry_run: bool = False,
        *,
        strategy: str = "deterministic",
        completer: object | None = None,
        ledger: object | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._deps = deps
        self._build = source_builder
        self._dry_run = dry_run
        self._strategy = strategy
        self._completer = completer
        self._ledger = ledger
        self._agent_ran: set[str] = set()
        # None -> settings default; 0 -> always poll (e.g. `watch --refresh`).
        self._ttl = (
            get_settings().feed_poll_ttl_seconds if ttl_seconds is None else max(0, ttl_seconds)
        )

    def run_once(self, providers: list[ProviderSpec]) -> WatchOutcome:
        init_schema()
        outcome = WatchOutcome()

        with session_scope() as session:
            for provider in providers:
                if not provider.enabled:
                    for feed in provider.feeds:
                        outcome.reports.append(
                            FeedReport(
                                provider.id, "—", feed.kind.value, "skipped", "provider disabled"
                            )
                        )
                    continue

                if not self._dry_run:
                    self._upsert_provider(session, provider)

                for feed in provider.feeds:
                    self._run_feed(session, provider, feed, outcome)

                if not self._dry_run:
                    session.commit()

        return outcome

    # -- caching ---------------------------------------------------------------

    def _cache_age(self, state: FeedState | None) -> float | None:
        """Seconds since the last successful poll if still fresh, else None.

        Never caches a failed poll, an unknown poll time, or a future timestamp
        (clock skew would otherwise freeze the feed indefinitely).
        """
        if state is None or self._ttl <= 0 or state.last_polled_at is None:
            return None
        if state.last_error:
            return None
        polled_at = state.last_polled_at
        if polled_at.tzinfo is None:
            polled_at = polled_at.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - polled_at).total_seconds()
        return age if 0 <= age < self._ttl else None

    def _cached_report(
        self, provider_id: str, feed_key: str, kind: str, state: FeedState, age: float
    ) -> FeedReport:
        at = f"; at {state.last_token}" if state.last_token else ""
        return FeedReport(
            provider_id,
            feed_key,
            kind,
            "cached",
            f"polled {age:.0f}s ago (ttl={self._ttl}s){at}",
        )

    # -- per-feed -------------------------------------------------------------

    def _run_feed(
        self, session: Session, provider: ProviderSpec, feed: FeedSpec, outcome: WatchOutcome
    ) -> None:
        if self._strategy == "agent":
            return self._run_provider_via_agent(session, provider, outcome)

        try:
            source = self._build(provider.id, feed, self._deps)
        except UnsupportedSourceKind as exc:
            outcome.reports.append(
                FeedReport(provider.id, "—", feed.kind.value, "unsupported", str(exc))
            )
            return
        except (ValueError, KeyError) as exc:
            outcome.reports.append(
                FeedReport(provider.id, "—", feed.kind.value, "failed", f"bad config: {exc}")
            )
            return

        state = (
            None
            if self._dry_run
            else self._get_or_create_state(session, provider.id, source.feed_key, source.kind.value)
        )

        age = self._cache_age(state)
        if age is not None:
            assert state is not None
            outcome.reports.append(
                self._cached_report(provider.id, source.feed_key, feed.kind.value, state, age)
            )
            return

        snapshot = (
            FeedStateSnapshot()
            if state is None
            else FeedStateSnapshot(
                last_token=state.last_token,
                last_payload_sha256=state.last_payload_sha256,
                last_payload_path=state.last_payload_path,
            )
        )

        try:
            poll = source.poll(snapshot)
        except Exception as exc:
            logger.exception("source %s raised", source.feed_key)
            outcome.reports.append(
                FeedReport(
                    provider.id, source.feed_key, feed.kind.value, "failed", f"unhandled: {exc}"
                )
            )
            if state is not None:
                state.last_error = f"unhandled: {exc}"
                state.last_polled_at = datetime.now(tz=UTC)
            return

        self._record(session, provider, source, poll, state, outcome)

    def _record(
        self,
        session: Session,
        provider: ProviderSpec,
        source: ChangeSource,
        poll: FeedPoll,
        state: FeedState | None,
        outcome: WatchOutcome,
    ) -> None:
        now = datetime.now(tz=UTC)
        kind = source.kind.value

        if not poll.ok:
            outcome.reports.append(
                FeedReport(provider.id, source.feed_key, kind, "failed", poll.error or "")
            )
            if state is not None:
                # last_error being set is what keeps this poll out of the cache.
                state.last_error = poll.error
                state.last_polled_at = now
            return

        if state is not None:
            state.last_error = None
            state.last_polled_at = now

        if poll.not_found:
            outcome.reports.append(
                FeedReport(provider.id, source.feed_key, kind, "not_found", poll.note or "")
            )
            return

        if not poll.changed:
            outcome.reports.append(
                FeedReport(
                    provider.id,
                    source.feed_key,
                    kind,
                    "unchanged",
                    f"at {poll.new_token}" if poll.new_token else "",
                )
            )
            return

        if state is not None:
            state.last_token = poll.new_token
            if poll.payload_sha256:
                state.last_payload_sha256 = poll.payload_sha256
            if poll.payload_path:
                state.last_payload_path = poll.payload_path

        if not poll.events:
            outcome.reports.append(
                FeedReport(
                    provider.id,
                    source.feed_key,
                    kind,
                    "baseline",
                    poll.note or f"recorded {poll.new_token}",
                )
            )
            return

        persisted = 0
        for event in poll.events:
            if state is not None and self._is_duplicate(session, event):
                outcome.duplicates += 1
                continue
            if state is not None:
                self._persist_event(session, event)
            outcome.events.append(event)
            persisted += 1

        first = poll.events[0]
        outcome.reports.append(
            FeedReport(
                provider.id,
                source.feed_key,
                kind,
                "changed",
                f"{first.old_token or '(new)'} → {poll.new_token}",
                event_count=persisted,
            )
        )

    # -- persistence ----------------------------------------------------------

    def _upsert_provider(self, session: Session, provider: ProviderSpec) -> None:
        row = session.get(Provider, provider.id)
        if row is None:
            session.add(
                Provider(
                    id=provider.id,
                    name=provider.name,
                    enabled=provider.enabled,
                    api_version_scheme=provider.api_version_scheme,
                )
            )
            session.flush()
            return
        row.name = provider.name
        row.enabled = provider.enabled
        row.api_version_scheme = provider.api_version_scheme

    def _get_or_create_state(
        self, session: Session, provider_id: str, feed_key: str, kind: str
    ) -> FeedState:
        row = session.scalar(
            select(FeedState).where(
                FeedState.provider_id == provider_id, FeedState.feed_key == feed_key
            )
        )
        if row is None:
            row = FeedState(provider_id=provider_id, feed_key=feed_key, kind=kind)
            session.add(row)
            session.flush()
        return row

    def _is_duplicate(self, session: Session, event: ChangeEvent) -> bool:
        return (
            session.scalar(
                select(ChangeEventRow.id).where(ChangeEventRow.dedupe_key == event.dedupe_key)
            )
            is not None
        )

    def _persist_event(self, session: Session, event: ChangeEvent) -> None:
        Watcher._persist_event_static(session, event)

    @staticmethod
    def _persist_event_static(session: Session, event: ChangeEvent) -> None:
        row = ChangeEventRow(
            provider_id=event.provider_id,
            feed_key=event.feed_key,
            source_kind=event.source_kind.value,
            source_url=event.source_url,
            old_token=event.old_token,
            new_token=event.new_token,
            title=event.title,
            summary=event.summary,
            body=event.body,
            body_url=event.body_url,
            severity=event.severity.value,
            dedupe_key=event.dedupe_key,
            detected_at=event.detected_at,
        )
        session.add(row)
        session.flush()
        for change in event.spec_changes:
            session.add(
                SpecChangeRow(
                    change_event_id=row.id,
                    kind=change.kind.value,
                    severity=change.severity.value,
                    direction=change.direction,
                    subject=change.subject,
                    pointer=change.pointer,
                    detail=change.detail or None,
                    before=change.before,
                    after=change.after,
                )
            )
        session.flush()

    # -- agent strategy -------------------------------------------------------

    def _run_provider_via_agent(
        self, session: Session, provider: ProviderSpec, outcome: WatchOutcome
    ) -> None:
        """One FeedAgent call per provider per run, cached by the same TTL —
        this is the LLM-backed path, where skipping a redundant poll saves money."""
        marker = f"agent:{provider.id}"
        if marker in self._agent_ran:
            return
        self._agent_ran.add(marker)

        if self._completer is None:
            outcome.reports.append(
                FeedReport(
                    provider.id,
                    marker,
                    "agent",
                    "failed",
                    "agent strategy requires an LLM but none is configured",
                )
            )
            return

        state = (
            None
            if self._dry_run
            else self._get_or_create_state(session, provider.id, marker, "agent")
        )
        age = self._cache_age(state)
        if age is not None:
            assert state is not None
            outcome.reports.append(self._cached_report(provider.id, marker, "agent", state, age))
            return

        from depfix.agent.scan_agent import ScanAgent
        from depfix.agent.scan_tools import ScanToolset

        toolset = ScanToolset(
            checkout_root=None,
            http=self._deps.http,
            npm=self._deps.npm,
            pypi=self._deps.pypi,
            github_api_url=self._deps.github_api_url,
        )
        now = datetime.now(tz=UTC)
        try:
            events, _transcript = ScanAgent(self._completer, ledger=self._ledger).poll_feed(  # type: ignore[arg-type]
                provider, toolset
            )
        except Exception as exc:
            logger.exception("agent feed poll for %s raised", provider.id)
            outcome.reports.append(
                FeedReport(provider.id, marker, "agent", "failed", f"unhandled: {exc}")
            )
            if state is not None:
                state.last_error = f"unhandled: {exc}"
                state.last_polled_at = now
            return

        if state is not None:
            state.last_error = None
            state.last_polled_at = now

        for event in events:
            if self._dry_run:
                outcome.events.append(event)
                continue
            if self._is_duplicate(session, event):
                outcome.duplicates += 1
                continue
            self._persist_event(session, event)
            outcome.events.append(event)

        outcome.reports.append(
            FeedReport(
                provider.id,
                marker,
                "agent",
                "changed" if events else "unchanged",
                f"agent found {len(events)} event(s)",
                event_count=len(events),
            )
        )
