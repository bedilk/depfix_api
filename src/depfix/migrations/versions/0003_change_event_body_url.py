"""Week 7: change_event.body_url -- links a joined release-notes body back to
the GitHub release page it came from.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("change_event", sa.Column("body_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("change_event", "body_url")
