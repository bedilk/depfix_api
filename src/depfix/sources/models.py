"""Data models for change detection.

A *feed* is one observable stream of change for one provider (an npm dist-tag,
a GitHub releases list, an OpenAPI document URL). Every feed reduces to the
same shape: an opaque ``token`` that identifies "where we were", plus zero or
more :class:`ChangeEvent` when that token moves.

Tokens are deliberately untyped strings. For npm it is a semver; for GitHub
releases a tag name; for OpenAPI the ``info.version`` field. The watcher never
interprets them — only the source that produced them does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    """Kinds of change feed.

    Implemented in Week 1: ``NPM_DIST_TAG``, ``GITHUB_RELEASE``, ``OPENAPI_SPEC``.
    The rest are declared so ``providers.yaml`` can be written forward — the
    watcher reports them as unimplemented rather than crashing.
    """

    NPM_DIST_TAG = "npm_dist_tag"
    PYPI_DIST_TAG = "pypi_dist_tag"
    GITHUB_RELEASE = "github_release"
    OPENAPI_SPEC = "openapi_spec"
    SMITHY_SPEC = "smithy_spec"
    ASYNCAPI_SPEC = "asyncapi_spec"
    REDIS_COMMANDS = "redis_commands"
    TS_EXPORTS_DIFF = "ts_exports_diff"
    SECURITY_ADVISORY = "security_advisory"  # NEW: OSV / GHSA vulnerability feed
    # Explicit opt-in local feed for deterministic end-to-end tests.
    FIXTURE_CHANGE = "fixture_change"
    RUBYGEMS_DIST_TAG = "rubygems_dist_tag"
    CHANGELOG_FILE = "changelog_file"  # Week 2
    CHANGELOG_HTML = "changelog_html"  # Week 2
    RSS = "rss"  # Week 2
    # Synthetic parent event for a migration re-imported from the local learned file.
    LEARNED_CATALOG = "learned_catalog"


class Severity(StrEnum):
    BREAKING = "breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    ADDITIVE = "additive"
    UNKNOWN = "unknown"


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.ADDITIVE: 0,
    Severity.UNKNOWN: 1,
    Severity.POTENTIALLY_BREAKING: 2,
    Severity.BREAKING: 3,
}


def max_severity(severities: list[Severity]) -> Severity:
    """Return the most severe entry, or ``UNKNOWN`` for an empty list."""
    if not severities:
        return Severity.UNKNOWN
    return max(severities, key=lambda s: _SEVERITY_RANK[s])


class SpecChangeKind(StrEnum):
    """Structural change detected between two OpenAPI documents."""

    PATH_REMOVED = "path_removed"
    OPERATION_REMOVED = "operation_removed"
    OPERATION_ADDED = "operation_added"
    OPERATION_ID_CHANGED = "operation_id_changed"
    OPERATION_DEPRECATED = "operation_deprecated"
    PARAM_REMOVED = "param_removed"
    PARAM_NOW_REQUIRED = "param_now_required"
    PARAM_TYPE_CHANGED = "param_type_changed"
    PROPERTY_REMOVED = "property_removed"
    PROPERTY_NOW_REQUIRED = "property_now_required"
    PROPERTY_TYPE_CHANGED = "property_type_changed"
    PROPERTY_ADDED = "property_added"
    ENUM_VALUE_REMOVED = "enum_value_removed"
    SCHEMA_REMOVED = "schema_removed"
    # message-driven (AsyncAPI) and command/export shapes
    OPERATION_MESSAGE_CHANGED = "operation_message_changed"
    COMMAND_REMOVED = "command_removed"
    COMMAND_ARITY_CHANGED = "command_arity_changed"
    EXPORT_REMOVED = "export_removed"


@dataclass(frozen=True)
class SpecChange:
    """One structural delta between two spec versions.

    ``direction`` is ``"request"`` | ``"response"`` | ``"n/a"`` and matters for
    severity: losing a response field breaks readers, losing a request field
    breaks writers, and the fix differs.
    """

    kind: SpecChangeKind
    severity: Severity
    subject: str  # e.g. "POST /v1/charges"
    pointer: str  # e.g. "paths./v1/charges.post.requestBody.amount"
    detail: str = ""
    direction: str = "n/a"
    before: str | None = None
    after: str | None = None

    def one_line(self) -> str:
        loc = f"{self.subject} · {self.pointer}" if self.subject else self.pointer
        return f"[{self.severity.value}] {self.kind.value}: {loc}" + (
            f" — {self.detail}" if self.detail else ""
        )


@dataclass(frozen=True)
class AdvisoryRecord:
    """One published vulnerability affecting a package.

    ``fixed_version`` is the earliest version that patches it, when the
    advisory names one; ``None`` means "no fix published yet", which is a
    real and important state (depfix can warn, but cannot bump to safety).
    ``affected_symbols`` is best-effort: advisories rarely name the exact
    vulnerable function, but when they do it drives reachability filtering.
    """

    advisory_id: str  # e.g. "GHSA-xxxx-yyyy-zzzz" or "CVE-2026-1234"
    package: str
    ecosystem: str  # "npm" | "pypi" | ...
    summary: str
    severity: str  # "critical" | "high" | "moderate" | "low"
    vulnerable_range: str  # human-readable, e.g. ">=3.0.0 <3.4.1"
    fixed_version: str | None
    references: tuple[str, ...] = ()
    affected_symbols: tuple[str, ...] = ()

    @property
    def has_fix(self) -> bool:
        return bool(self.fixed_version)


@dataclass
class ChangeEvent:
    """A detected change on one feed.

    ``dedupe_key`` is what stops us reopening the same PR forever (Week 6). It
    is a function of (provider, feed, new token) only — deliberately *not* of
    the body text, so a provider editing their release notes does not produce a
    second event.
    """

    provider_id: str
    feed_key: str
    source_kind: SourceKind
    source_url: str | None = None
    old_token: str | None = None
    new_token: str = ""
    title: str = ""
    summary: str = ""
    body: str = ""  # raw notes / markdown, unparsed. Week 2 classifies this.
    body_url: str | None = None  # link to the release-notes page `body` came from
    severity: Severity = Severity.UNKNOWN
    spec_changes: list[SpecChange] = field(default_factory=list)
    advisory: AdvisoryRecord | None = None  # NEW: set by the security feed
    detected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        payload = f"{self.provider_id}|{self.feed_key}|{self.new_token}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def breaking_count(self) -> int:
        return sum(1 for c in self.spec_changes if c.severity is Severity.BREAKING)


@dataclass(frozen=True)
class FeedStateSnapshot:
    """What we knew about a feed before this poll."""

    last_token: str | None = None
    last_payload_sha256: str | None = None
    last_payload_path: str | None = None


@dataclass
class FeedPoll:
    """Outcome of polling a single feed.

    ``changed`` and ``events`` are decoupled on purpose: the first time we see
    an OpenAPI document there is nothing to diff against, so state must be
    persisted (``changed=True``) with no event emitted.
    """

    changed: bool = False
    new_token: str | None = None
    payload_sha256: str | None = None
    payload_path: str | None = None
    events: list[ChangeEvent] = field(default_factory=list)
    error: str | None = None
    not_found: bool = False
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def unchanged(
        cls, token: str | None = None, sha: str | None = None, path: str | None = None
    ) -> FeedPoll:
        return cls(changed=False, new_token=token, payload_sha256=sha, payload_path=path)

    @classmethod
    def failure(cls, reason: str) -> FeedPoll:
        return cls(changed=False, error=reason)

    @classmethod
    def missing(cls, reason: str = "not found") -> FeedPoll:
        return cls(changed=False, not_found=True, note=reason)

    @classmethod
    def baseline(cls, token: str, sha: str | None = None, path: str | None = None) -> FeedPoll:
        return cls(
            changed=True,
            new_token=token,
            payload_sha256=sha,
            payload_path=path,
            note="baseline recorded (nothing to diff against)",
        )
