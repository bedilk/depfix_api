"""Persist the change decision derived from each repository scan.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repo_scan_change",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_scan_id",
            sa.Integer(),
            sa.ForeignKey("repo_scan.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "breaking_change_id",
            sa.Integer(),
            sa.ForeignKey("breaking_change.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("matched_call_site_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_symbols", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo_scan_id", "breaking_change_id", name="uq_repo_scan_change_pair"),
    )
    op.create_index("ix_repo_scan_change_repo_scan_id", "repo_scan_change", ["repo_scan_id"])
    op.create_index(
        "ix_repo_scan_change_breaking_change_id", "repo_scan_change", ["breaking_change_id"]
    )
    op.create_index("ix_repo_scan_change_status", "repo_scan_change", ["status"])


def downgrade() -> None:
    op.drop_index("ix_repo_scan_change_status", table_name="repo_scan_change")
    op.drop_index("ix_repo_scan_change_breaking_change_id", table_name="repo_scan_change")
    op.drop_index("ix_repo_scan_change_repo_scan_id", table_name="repo_scan_change")
    op.drop_table("repo_scan_change")
