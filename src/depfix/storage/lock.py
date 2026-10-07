"""Postgres advisory lock for the fleet orchestrator's single-writer
invariant.

A cron-triggered ``depfix pipeline`` run must never overlap with another
one still in flight -- two orchestrators racing on the same repo would
double-clone, double-fix, and could open duplicate PRs before either one's
ledger write lands. ``pg_try_advisory_lock`` gives us that mutual exclusion
for free, scoped to one Postgres connection for the run's lifetime, with no
extra infrastructure (no Redis, no lock table to clean up after a crash --
Postgres releases the lock itself when the connection closes, even if the
process is killed).

No-ops (always "acquired") on SQLite, since the local/test database is
never shared across concurrent processes the way the production Postgres
one is -- see ``in_memory_db`` in ``tests/unit/test_orchestrator.py``, which
relies on this to avoid needing a real Postgres in unit tests.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

#: Fixed key identifying "the depfix fleet orchestrator" lock class --
#: pg_try_advisory_lock's key space is global to the database, so this only
#: needs to be unique enough not to collide with some other app's unrelated
#: use of advisory locks, not cryptographically random.
ORCHESTRATOR_LOCK_KEY = zlib.crc32(b"depfix:orchestrator")


@contextmanager
def advisory_lock(engine: Engine, key: int = ORCHESTRATOR_LOCK_KEY) -> Iterator[bool]:
    """Hold a session-level Postgres advisory lock for the duration of the
    ``with`` block.

    Yields ``True`` if the lock was acquired, ``False`` if another
    connection already holds it -- in which case the caller should skip
    this run entirely rather than block. A cron tick that loses the race
    should just wait for the next tick, not queue up behind the one
    currently running.

    On any non-Postgres dialect (SQLite in tests/dev), always yields
    ``True`` without taking a real lock -- there's nothing else connected
    to a throwaway SQLite file to race against.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return

    conn = engine.connect()
    try:
        acquired = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
        if not acquired:
            logger.warning("orchestrator advisory lock %d already held; skipping this run", key)
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        conn.close()
