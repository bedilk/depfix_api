"""SQLAlchemy declarative schema.

``tracked_package`` / ``version_event`` (Week 0's single-version-string
model) are gone as of Week 2 — see ``docs/decisions.md``. Everything below is
provider / feed / change-event / breaking-change, keyed off Week 1's
multi-source model.

As of Week 7, this is the ORM's own view of the schema -- the source of
truth for tests (SQLite, via ``Base.metadata.create_all``) and for
autogenerate diffs. Production Postgres schema *changes* now go through
versioned Alembic migrations in ``depfix.migrations`` (see
:func:`depfix.storage.db.init_schema`), not ``create_all``/manual ALTERs --
``_ensure_classified_at_column`` in ``storage/db.py`` survives only as a
defensive no-op for pre-Alembic deployments.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Common declarative base for all depfix tables."""


# ============================================================================
#  Week 1 — provider / feed / change-event tables
# ============================================================================


class Provider(Base):
    """A vendor we watch. Mirrors one entry in providers.yaml."""

    __tablename__ = "provider"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    api_version_scheme: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    feeds: Mapped[list[FeedState]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class FeedState(Base):
    """Last-known position of one feed. The compare-and-store cursor.

    ``last_payload_path`` points at the on-disk cache of the previous OpenAPI
    document. The spec body is not stored here: Stripe's is ~6 MB, we need the
    whole prior document to diff, and it is never queried by column.
    """

    __tablename__ = "feed_state"
    __table_args__ = (
        UniqueConstraint("provider_id", "feed_key", name="uq_feed_state_provider_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("provider.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feed_key: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    last_token: Mapped[str | None] = mapped_column(String(255), default=None)
    last_payload_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    last_payload_path: Mapped[str | None] = mapped_column(Text, default=None)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    provider: Mapped[Provider] = relationship(back_populates="feeds")

    def __repr__(self) -> str:  # pragma: no cover
        return f"FeedState({self.provider_id}/{self.feed_key} @ {self.last_token})"


class ChangeEventRow(Base):
    """A detected change. Week 2's classifier reads from here.

    ``dedupe_key`` is unique and is the Week 6 idempotency anchor — it is what
    stops us reopening the same PR on every cron tick.
    """

    __tablename__ = "change_event"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_change_event_dedupe"),
        Index("ix_change_event_provider_detected", "provider_id", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("provider.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feed_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    old_token: Mapped[str | None] = mapped_column(String(255), default=None)
    new_token: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    body: Mapped[str | None] = mapped_column(Text, default=None)
    body_url: Mapped[str | None] = mapped_column(Text, default=None)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Set once `depfix classify` has processed this event. Nullable + set
    # post-hoc rather than a status enum: classification can be re-run (e.g.
    # after a prompt change) without needing a "pending" -> "done" state
    # machine — the presence of BreakingChangeRow children is the real state.
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    spec_changes: Mapped[list[SpecChangeRow]] = relationship(
        back_populates="change_event", cascade="all, delete-orphan"
    )
    breaking_changes: Mapped[list[BreakingChangeRow]] = relationship(
        back_populates="change_event", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ChangeEventRow({self.provider_id} {self.old_token} -> "
            f"{self.new_token} [{self.severity}])"
        )


class SpecChangeRow(Base):
    """One structural delta from an OpenAPI diff.

    Queryable because this is the table that answers the provider-side pitch:
    "which of your changes break the most call sites". Do not collapse it into
    a JSON blob on change_event.
    """

    __tablename__ = "spec_change"
    __table_args__ = (Index("ix_spec_change_kind_severity", "kind", "severity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_event_id: Mapped[int] = mapped_column(
        ForeignKey("change_event.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="n/a")
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    pointer: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    before: Mapped[str | None] = mapped_column(Text, default=None)
    after: Mapped[str | None] = mapped_column(Text, default=None)

    change_event: Mapped[ChangeEventRow] = relationship(back_populates="spec_changes")


# ============================================================================
#  Week 2 — classification output
# ============================================================================


class BreakingChangeRow(Base):
    """A classified, actionable breaking change — what ``depfix run`` consumes.

    ``dedupe_key`` (see ``core.models.BreakingChange.dedupe_key``) is unique
    so re-running ``depfix classify`` on the same event is idempotent rather
    than inserting duplicate rows. Indexed on ``(kind, source)`` because that
    is the eval/reporting axis: "how many PARAM_REMOVED changes came from
    spec_diff vs release_notes".
    """

    __tablename__ = "breaking_change"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_breaking_change_dedupe"),
        Index("ix_breaking_change_kind_source", "kind", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_event_id: Mapped[int] = mapped_column(
        ForeignKey("change_event.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    package: Mapped[str] = mapped_column(String(214), nullable=False)
    old_version: Mapped[str] = mapped_column(String(128), nullable=False)
    new_version: Mapped[str] = mapped_column(String(128), nullable=False)
    old_api: Mapped[str] = mapped_column(Text, nullable=False)
    new_api: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    migration_guide: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    evidence: Mapped[str | None] = mapped_column(Text, default=None)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    call_site_hints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    severity_evidence: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    change_event: Mapped[ChangeEventRow] = relationship(back_populates="breaking_changes")
    scan_matches: Mapped[list[ScanChangeMatchRow]] = relationship(
        back_populates="breaking_change", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"BreakingChangeRow({self.package} {self.kind} {self.old_api!r})"


# ============================================================================
#  Week 3 — repo / call-site scan tables
# ============================================================================


class RepoRow(Base):
    """A repo depfix has scanned at least once. ``id`` is ``owner/name`` --
    stable, human-readable, and already unique on GitHub, so there is no
    reason to mint a surrogate key for it.
    """

    __tablename__ = "repo"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    installation_id: Mapped[int | None] = mapped_column(Integer, default=None)
    default_branch: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    scans: Mapped[list[RepoScanRow]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    fix_runs: Mapped[list[FixRunRow]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    change_attempts: Mapped[list[ChangeAttemptRow]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"RepoRow({self.id})"


class RepoScanRow(Base):
    """One run of the call-site scanner against one repo at one commit.

    Kept separate from ``call_site`` (one row per scan, many rows per scan's
    call sites) so "how many actionable sites did the last scan of this repo
    find" is a single indexed lookup, not a per-scan aggregate query.
    """

    __tablename__ = "repo_scan"
    __table_args__ = (Index("ix_repo_scan_repo_scanned", "repo_id", "scanned_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False, index=True
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    scanner_version: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    files_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_matches: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    dependencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Non-empty when this scan was narrowed to one change's symbols. A narrowed
    #: scan is NOT a superset, so plan must not reuse it for another change.
    narrowed_symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    errors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    repo: Mapped[RepoRow] = relationship(back_populates="scans")
    call_sites: Mapped[list[CallSiteRow]] = relationship(
        back_populates="repo_scan", cascade="all, delete-orphan"
    )
    change_matches: Mapped[list[ScanChangeMatchRow]] = relationship(
        back_populates="repo_scan", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"RepoScanRow({self.repo_id}@{self.commit_sha[:8]})"


class CallSiteRow(Base):
    """One matched call site from one scan.

    Indexed on ``(repo_scan_id, confidence)`` because the load-bearing query
    is "give me this scan's actionable (HIGH/MEDIUM) sites" -- the same
    HIGH/MEDIUM-only filter ``RepoScanResult.actionable_sites`` applies
    in-memory during a scan.
    """

    __tablename__ = "call_site"
    __table_args__ = (Index("ix_call_site_scan_confidence", "repo_scan_id", "confidence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_scan_id: Mapped[int] = mapped_column(
        ForeignKey("repo_scan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filepath: Mapped[str] = mapped_column(Text, nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    column: Mapped[int] = mapped_column(Integer, nullable=False)
    line_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    severity_evidence: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    context_before: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_after: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    repo_scan: Mapped[RepoScanRow] = relationship(back_populates="call_sites")

    def __repr__(self) -> str:  # pragma: no cover
        return f"CallSiteRow({self.filepath}:{self.line_number} {self.symbol})"


class ScanChangeMatchRow(Base):
    """The decision made when one scan is compared with one upstream change.

    ``repo_scan`` stores raw observations. This row stores the derived,
    commit-specific decision that makes a change eligible for ``plan`` while
    preserving why it was rejected or considered actionable.
    """

    __tablename__ = "repo_scan_change"
    __table_args__ = (
        UniqueConstraint("repo_scan_id", "breaking_change_id", name="uq_repo_scan_change_pair"),
        Index("ix_repo_scan_change_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_scan_id: Mapped[int] = mapped_column(
        ForeignKey("repo_scan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    breaking_change_id: Mapped[int] = mapped_column(
        ForeignKey("breaking_change.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    matched_call_site_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    repo_scan: Mapped[RepoScanRow] = relationship(back_populates="change_matches")
    breaking_change: Mapped[BreakingChangeRow] = relationship(back_populates="scan_matches")

    def __repr__(self) -> str:  # pragma: no cover
        return f"ScanChangeMatchRow({self.repo_scan_id}->{self.breaking_change_id} {self.status})"


# ============================================================================
#  Week 4 — fix generation + test verification
# ============================================================================


class FixRunRow(Base):
    """One run of :class:`depfix.core.pipeline.FixPipeline` against one repo,
    for one breaking change.

    Breaking-change fields are denormalized rather than a hard FK to
    ``breaking_change`` -- same rationale as ``RepoScanRow`` denormalizing
    ``provider_id`` instead of FK'ing to ``provider``: a manually-supplied
    change (``depfix fix --change-file``) may never have been persisted as
    a ``BreakingChangeRow`` at all.
    """

    __tablename__ = "fix_run"
    __table_args__ = (Index("ix_fix_run_repo_started", "repo_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo_scan_id: Mapped[int | None] = mapped_column(
        ForeignKey("repo_scan.id", ondelete="SET NULL"), default=None
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    package: Mapped[str] = mapped_column(String(214), nullable=False, default="")
    old_api: Mapped[str] = mapped_column(Text, nullable=False, default="")
    new_api: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    files_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_usages_fixed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_classify: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_characterization: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_call_site_judge: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Whether Verifier actually ran the repo's own test suite (as opposed to
    # skipping -- no test script, install failure, ...); see
    # `verification_skipped_reason` for why, when this is False.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_skipped_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    typechecked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    repo: Mapped[RepoRow] = relationship(back_populates="fix_runs")
    file_fixes: Mapped[list[FileFixRow]] = relationship(
        back_populates="fix_run", cascade="all, delete-orphan"
    )
    test_runs: Mapped[list[TestRunRow]] = relationship(
        back_populates="fix_run", cascade="all, delete-orphan"
    )
    pull_request: Mapped[PullRequestRow | None] = relationship(
        back_populates="fix_run", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"FixRunRow({self.repo_id} {self.old_api!r} -> {self.new_api!r})"


class FileFixRow(Base):
    """One file's edit within a fix run -- mirrors
    :class:`depfix.apply.models.FileEdit`."""

    __tablename__ = "file_fix"
    __table_args__ = (
        Index("ix_file_fix_run_verdict", "fix_run_id", "verdict"),
        Index("ix_file_fix_origin", "origin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fix_run_id: Mapped[int] = mapped_column(
        ForeignKey("fix_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    usages_fixed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diff: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    codemod_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    fix_run: Mapped[FixRunRow] = relationship(back_populates="file_fixes")

    def __repr__(self) -> str:  # pragma: no cover
        return f"FileFixRow({self.relpath} {self.verdict})"


class TestRunRow(Base):
    """One baseline or after-fix test run within a fix run's verification
    step -- one row each, uniquely keyed on ``(fix_run_id, phase)`` since a
    :class:`depfix.verify.verifier.Verifier` run produces at most one of
    each. See :class:`depfix.verify.models.TestRunResult`.
    """

    __tablename__ = "test_run"
    __table_args__ = (UniqueConstraint("fix_run_id", "phase", name="uq_test_run_fix_run_phase"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fix_run_id: Mapped[int] = mapped_column(
        ForeignKey("fix_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)  # "baseline" | "after_fix"
    framework: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    passed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Failed test identities (`TestCase.identity`), not full case objects --
    # enough to eyeball what regressed without persisting every case's full
    # message text on every run.
    failed_identities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    used_fallback_parser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parse_error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    fix_run: Mapped[FixRunRow] = relationship(back_populates="test_runs")

    def __repr__(self) -> str:  # pragma: no cover
        return f"TestRunRow({self.phase} {self.passed_count}p/{self.failed_count}f)"


# ============================================================================
#  Week 5 — PR creation
# ============================================================================


class PullRequestRow(Base):
    """The PR (if any) opened for a fix run -- at most one per
    ``fix_run``, hence the unique FK rather than a one-to-many.
    """

    __tablename__ = "pull_request"
    __table_args__ = (
        UniqueConstraint("fix_run_id", name="uq_pull_request_fix_run"),
        Index("ix_pull_request_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fix_run_id: Mapped[int] = mapped_column(
        ForeignKey("fix_run.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    html_url: Mapped[str] = mapped_column(Text, nullable=False)
    head_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    already_existed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    merged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    fix_run: Mapped[FixRunRow] = relationship(back_populates="pull_request")

    def __repr__(self) -> str:  # pragma: no cover
        return f"PullRequestRow(#{self.number} {self.state})"


# ============================================================================
#  Week 6 — fleet orchestrator idempotency ledger
# ============================================================================


class AttemptStatus(enum.StrEnum):
    """Outcome of one ``(repo, breaking change)`` combination the fleet
    orchestrator has attempted.

    Only ``PR_OPENED`` and ``IGNORED`` are terminal (see ``is_terminal``) --
    everything else is a reason a later cron tick may legitimately want to
    try again (a transient failure, a fix that produced no kept edits this
    time, ...). ``NO_CALL_SITES`` in particular is not just non-terminal but
    deliberately cheap to recheck: see ``ChangeAttemptRow.last_checked_ref_sha``.
    """

    PR_OPENED = "pr_opened"
    FIXED_NO_PR = "fixed_no_pr"
    NO_CALL_SITES = "no_call_sites"
    VERSION_NOT_AFFECTED = "version_not_affected"
    #: Upstream removed an API this repo still calls, with no replacement.
    #: Information only: never fixed, never a PR. Rechecked when HEAD moves.
    REPORT_ONLY = "report_only"
    NO_KEPT_EDITS = "no_kept_edits"
    FAILED = "failed"
    IGNORED = "ignored"

    @property
    def is_terminal(self) -> bool:
        return self in (AttemptStatus.PR_OPENED, AttemptStatus.IGNORED)


class ChangeAttemptRow(Base):
    """Idempotency ledger: at most one row per ``(repo_id, dedupe_key)`` --
    the anchor that stops the fleet orchestrator from reopening a PR or
    re-fixing a change a human already told it to ignore on every cron
    tick. See ``AttemptStatus``.
    """

    __tablename__ = "change_attempt"
    __table_args__ = (
        UniqueConstraint("repo_id", "dedupe_key", name="uq_change_attempt_repo_dedupe"),
        Index("ix_change_attempt_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fix_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("fix_run.id", ondelete="SET NULL"), default=None
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # HEAD sha of the repo's default branch as of the last time this row was
    # (re)checked -- lets a NO_CALL_SITES outcome be skipped cheaply until
    # the repo actually changes, instead of being re-scanned on every tick.
    last_checked_ref_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    repo: Mapped[RepoRow] = relationship(back_populates="change_attempts")
    fix_run: Mapped[FixRunRow | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"ChangeAttemptRow({self.repo_id} {self.dedupe_key[:12]} {self.status})"
