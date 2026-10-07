"""Alembic migrations for depfix's Postgres schema.

Lives inside the ``depfix`` package (rather than a top-level ``migrations/``
directory) so Docker's ``COPY src ./src`` -- and any ``pip install``,
editable or not -- ships these files automatically with no separate
packaging step. See ``storage/db.py`` for how the fleet orchestrator
resolves this directory at run time, and ``alembic.ini`` at the repo root
for the developer CLI entry point.

SQLite (dev/test) never runs these -- it stays on ``Base.metadata.create_all``,
since there's no shared server whose schema needs versioning, and every
test's in-memory database is thrown away at the end of the test anyway.
"""

from __future__ import annotations
