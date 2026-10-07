"""Week 6: change_attempt idempotency ledger.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_attempt",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.String(length=255),
            sa.ForeignKey("repo.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts_used", sa.Integer(), nullable=False),
        sa.Column(
            "fix_run_id",
            sa.Integer(),
            sa.ForeignKey("fix_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("last_checked_ref_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_id", "dedupe_key", name="uq_change_attempt_repo_dedupe"),
    )
    op.create_index("ix_change_attempt_repo_id", "change_attempt", ["repo_id"])
    op.create_index("ix_change_attempt_status", "change_attempt", ["status"])


def downgrade() -> None:
    op.drop_table("change_attempt")
