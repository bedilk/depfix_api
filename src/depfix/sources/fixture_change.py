"""Explicit opt-in deterministic feed for end-to-end seed tests."""

from __future__ import annotations

from typing import Any, ClassVar

from depfix.sources.base import ChangeSource
from depfix.sources.models import (
    ChangeEvent,
    FeedPoll,
    FeedStateSnapshot,
    Severity,
    SourceKind,
    SpecChange,
    SpecChangeKind,
)


class FixtureChangeSource(ChangeSource):
    """Emit a configured structural event once, then retain its cursor."""

    kind: ClassVar[SourceKind] = SourceKind.FIXTURE_CHANGE

    def __init__(self, provider_id: str, config: dict[str, Any]) -> None:
        super().__init__(provider_id, config)
        self.fixture_id = str(config.get("id") or provider_id)
        self.old_token = str(self._require("old_token"))
        self.new_token = str(self._require("new_token"))
        raw_changes = self._require("spec_changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValueError("fixture_change: 'spec_changes' must be a non-empty list")
        try:
            self.changes = [
                SpecChange(
                    kind=SpecChangeKind(item["kind"]),
                    severity=Severity(item.get("severity", "breaking")),
                    subject=item["subject"],
                    pointer=item.get("pointer", item["subject"]),
                    detail=item.get("detail", ""),
                    direction=item.get("direction", "n/a"),
                    before=item.get("before"),
                    after=item.get("after"),
                )
                for item in raw_changes
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"fixture_change: invalid spec_changes entry: {exc}") from exc

    @property
    def feed_key(self) -> str:
        return f"fixture_change:{self.fixture_id}"

    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        if state.last_token == self.new_token:
            return FeedPoll.unchanged(token=self.new_token)
        event = ChangeEvent(
            provider_id=self.provider_id,
            feed_key=self.feed_key,
            source_kind=self.kind,
            source_url=self.config.get("source_url"),
            old_token=state.last_token or self.old_token,
            new_token=self.new_token,
            title=self.config.get("title", f"Fixture change {self.new_token}"),
            summary="Deterministic local test change",
            severity=Severity.BREAKING,
            spec_changes=self.changes,
            raw={"fixture": True},
        )
        return FeedPoll(changed=True, new_token=self.new_token, events=[event])
