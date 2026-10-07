"""Add severity labels and their evidence provenance.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    for table in ("breaking_change", "call_site"):
        op.add_column(table, sa.Column("severity", sa.String(16), nullable=True))
        op.add_column(table, sa.Column("severity_evidence", sa.String(32), nullable=True))


def downgrade() -> None:
    for table in ("breaking_change", "call_site"):
        op.drop_column(table, "severity_evidence")
        op.drop_column(table, "severity")
