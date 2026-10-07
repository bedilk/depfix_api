"""Week 7: edit provenance and escalation.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_fix", sa.Column("origin", sa.String(16), nullable=False, server_default="llm")
    )
    op.add_column(
        "file_fix", sa.Column("codemod_id", sa.String(64), nullable=False, server_default="")
    )
    op.add_column(
        "fix_run", sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_index("ix_file_fix_origin", "file_fix", ["origin"])


def downgrade() -> None:
    op.drop_index("ix_file_fix_origin", table_name="file_fix")
    op.drop_column("fix_run", "escalated")
    op.drop_column("file_fix", "codemod_id")
    op.drop_column("file_fix", "origin")
