"""Add per-stage LLM cost columns to fix_run.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fix_run", sa.Column("cost_classify", sa.Float(), nullable=False, server_default="0")
    )
    op.add_column(
        "fix_run",
        sa.Column("cost_characterization", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "fix_run", sa.Column("cost_call_site_judge", sa.Float(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    for col in ("cost_call_site_judge", "cost_characterization", "cost_classify"):
        op.drop_column("fix_run", col)
