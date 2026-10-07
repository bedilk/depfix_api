"""Week 7: confidence tier and typecheck evidence on fix runs.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fix_run",
        sa.Column("confidence_tier", sa.String(16), nullable=False, server_default="none"),
    )
    op.add_column(
        "fix_run", sa.Column("typechecked", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("fix_run", "typechecked")
    op.drop_column("fix_run", "confidence_tier")
