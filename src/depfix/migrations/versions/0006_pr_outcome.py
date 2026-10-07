"""Capture merged/closed PR outcomes observed by reconciliation.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pull_request", sa.Column("state", sa.String(16), nullable=False, server_default="open")
    )
    op.add_column(
        "pull_request", sa.Column("merged", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("pull_request", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pull_request", sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "pull_request", sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_pull_request_state", "pull_request", ["state"])


def downgrade() -> None:
    op.drop_index("ix_pull_request_state", table_name="pull_request")
    op.drop_column("pull_request", "last_reconciled_at")
    op.drop_column("pull_request", "merged_at")
    op.drop_column("pull_request", "closed_at")
    op.drop_column("pull_request", "merged")
    op.drop_column("pull_request", "state")
