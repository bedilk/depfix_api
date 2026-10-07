"""Baseline: every table through Week 5 (everything except change_attempt).

Revision ID: 0001
Revises:
Create Date: 2026-09-11

Formalizes what ``Base.metadata.create_all`` has been creating ad hoc since
Week 1 -- see ``docs/decisions.md``'s repeated "Alembic still deferred"
notes. Fresh Postgres databases run this (then later revisions) in order via
``alembic upgrade head``. ``depfix.storage.db`` detects complete
pre-Alembic schemas created by ``create_all`` and stamps their matching
revision before upgrading, so existing deployments do not recreate tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("api_version_scheme", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "feed_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "provider_id",
            sa.String(length=64),
            sa.ForeignKey("provider.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feed_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("last_token", sa.String(length=255), nullable=True),
        sa.Column("last_payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_payload_path", sa.Text(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_id", "feed_key", name="uq_feed_state_provider_key"),
    )
    op.create_index("ix_feed_state_provider_id", "feed_state", ["provider_id"])

    op.create_table(
        "change_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "provider_id",
            sa.String(length=64),
            sa.ForeignKey("provider.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feed_key", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("old_token", sa.String(length=255), nullable=True),
        sa.Column("new_token", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedupe_key", name="uq_change_event_dedupe"),
    )
    op.create_index("ix_change_event_provider_id", "change_event", ["provider_id"])
    op.create_index(
        "ix_change_event_provider_detected", "change_event", ["provider_id", "detected_at"]
    )

    op.create_table(
        "spec_change",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "change_event_id",
            sa.Integer(),
            sa.ForeignKey("change_event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("pointer", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
    )
    op.create_index("ix_spec_change_change_event_id", "spec_change", ["change_event_id"])
    op.create_index("ix_spec_change_kind_severity", "spec_change", ["kind", "severity"])

    op.create_table(
        "breaking_change",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "change_event_id",
            sa.Integer(),
            sa.ForeignKey("change_event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("package", sa.String(length=214), nullable=False),
        sa.Column("old_version", sa.String(length=128), nullable=False),
        sa.Column("new_version", sa.String(length=128), nullable=False),
        sa.Column("old_api", sa.Text(), nullable=False),
        sa.Column("new_api", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("migration_guide", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("call_site_hints", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_breaking_change_dedupe"),
    )
    op.create_index("ix_breaking_change_change_event_id", "breaking_change", ["change_event_id"])
    op.create_index("ix_breaking_change_kind_source", "breaking_change", ["kind", "source"])

    op.create_table(
        "repo",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("installation_id", sa.Integer(), nullable=True),
        sa.Column("default_branch", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "repo_scan",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.String(length=255),
            sa.ForeignKey("repo.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("scanner_version", sa.String(length=16), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("files_scanned", sa.Integer(), nullable=False),
        sa.Column("manifest_matches", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_repo_scan_repo_id", "repo_scan", ["repo_id"])
    op.create_index("ix_repo_scan_repo_scanned", "repo_scan", ["repo_id", "scanned_at"])

    op.create_table(
        "call_site",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_scan_id",
            sa.Integer(),
            sa.ForeignKey("repo_scan.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filepath", sa.Text(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("column", sa.Integer(), nullable=False),
        sa.Column("line_content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
    )
    op.create_index("ix_call_site_repo_scan_id", "call_site", ["repo_scan_id"])
    op.create_index("ix_call_site_scan_confidence", "call_site", ["repo_scan_id", "confidence"])

    op.create_table(
        "fix_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.String(length=255),
            sa.ForeignKey("repo.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "repo_scan_id",
            sa.Integer(),
            sa.ForeignKey("repo_scan.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("package", sa.String(length=214), nullable=False),
        sa.Column("old_api", sa.Text(), nullable=False),
        sa.Column("new_api", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("files_scanned", sa.Integer(), nullable=False),
        sa.Column("files_affected", sa.Integer(), nullable=False),
        sa.Column("total_usages_fixed", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verification_skipped_reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fix_run_repo_id", "fix_run", ["repo_id"])
    op.create_index("ix_fix_run_repo_started", "fix_run", ["repo_id", "started_at"])

    op.create_table(
        "file_fix",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "fix_run_id",
            sa.Integer(),
            sa.ForeignKey("fix_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relpath", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("usages_fixed", sa.Integer(), nullable=False),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
    )
    op.create_index("ix_file_fix_fix_run_id", "file_fix", ["fix_run_id"])
    op.create_index("ix_file_fix_run_verdict", "file_fix", ["fix_run_id", "verdict"])

    op.create_table(
        "test_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "fix_run_id",
            sa.Integer(),
            sa.ForeignKey("fix_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("framework", sa.String(length=16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_identities", sa.JSON(), nullable=False),
        sa.Column("used_fallback_parser", sa.Boolean(), nullable=False),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=False),
        sa.UniqueConstraint("fix_run_id", "phase", name="uq_test_run_fix_run_phase"),
    )
    op.create_index("ix_test_run_fix_run_id", "test_run", ["fix_run_id"])

    op.create_table(
        "pull_request",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "fix_run_id",
            sa.Integer(),
            sa.ForeignKey("fix_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("html_url", sa.Text(), nullable=False),
        sa.Column("head_branch", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("already_existed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fix_run_id", name="uq_pull_request_fix_run"),
    )


def downgrade() -> None:
    op.drop_table("pull_request")
    op.drop_table("test_run")
    op.drop_table("file_fix")
    op.drop_table("fix_run")
    op.drop_table("call_site")
    op.drop_table("repo_scan")
    op.drop_table("repo")
    op.drop_table("breaking_change")
    op.drop_table("spec_change")
    op.drop_table("change_event")
    op.drop_table("feed_state")
    op.drop_table("provider")
