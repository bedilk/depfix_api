"""The ``ChangeSource`` seam.

Every feed kind implements exactly two things: a stable ``feed_key`` and
``poll(state) -> FeedPoll``. Sources are stateless — the watcher owns
persistence — which makes each one testable with a stubbed HTTP client and no
database.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from depfix.sources.models import FeedPoll, FeedStateSnapshot, SourceKind


class UnsupportedSourceKind(RuntimeError):
    """Raised for a feed kind declared in providers.yaml but not implemented."""


class ChangeSource(ABC):
    kind: ClassVar[SourceKind]

    def __init__(self, provider_id: str, config: dict[str, Any]) -> None:
        self.provider_id = provider_id
        self.config = config

    @property
    @abstractmethod
    def feed_key(self) -> str:
        """Stable identity for this feed. Must not change between releases —
        it is the primary key of persisted state."""

    @abstractmethod
    def poll(self, state: FeedStateSnapshot) -> FeedPoll:
        """Compare the live feed against ``state``. Must not raise for
        expected failures (network, 404, malformed payload) — return a
        ``FeedPoll`` with ``error`` or ``not_found`` set instead."""

    def _require(self, key: str) -> Any:
        if key not in self.config or self.config[key] in (None, ""):
            raise ValueError(f"{self.kind.value}: missing required config '{key}'")
        return self.config[key]

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.feed_key!r})"
