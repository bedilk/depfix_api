"""SQLAlchemy engine and session lifecycle for depfix.

Engine construction is lazy and cached: the first call to :func:`get_engine`
reads ``DATABASE_URL`` from :class:`depfix.config.Settings`. Tests point
``DATABASE_URL`` at a temporary SQLite file (or monkey-patch the module-level
``_engine`` / ``_session_factory``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from depfix.config import get_settings
from depfix.storage.schema import Base

logger = logging.getLogger(__name__)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

# Tables created by ``0001_baseline``.  depfix created these directly with
# ``Base.metadata.create_all`` before it adopted Alembic, so a running
# pre-Alembic deployment has tables but no ``alembic_version`` record.
_BASELINE_TABLES = frozenset(
    {
        "provider",
        "feed_state",
        "change_event",
        "spec_change",
        "breaking_change",
        "repo",
        "repo_scan",
        "call_site",
        "fix_run",
        "file_fix",
        "test_run",
        "pull_request",
    }
)


def get_engine() -> Engine:
    """Return the process-wide engine, building it on first use.

    Local development defaults to SQLite.  A configured local Postgres URL
    also falls back to the same SQLite file when its server is unavailable,
    which keeps CLI commands usable after a Docker service stops.  Remote
    Postgres URLs never fall back: an explicit production database outage
    must remain visible to its operator.
    """
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().database_url
        _engine = _engine_for_url(url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
        logger.debug("SQLAlchemy engine created (driver=%s)", _engine.dialect.name)
    return _engine


def _engine_for_url(url: str) -> Engine:
    """Build an engine, falling back only from an unreachable local Postgres."""
    engine = create_engine(url, future=True)
    if not _is_local_postgres_url(url):
        return engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        fallback_url = f"sqlite:///{Path('depfix.db').resolve()}"
        logger.warning(
            "Local Postgres unavailable (%s); falling back to SQLite at %s. "
            "Start Postgres or set DATABASE_URL=sqlite:///depfix.db to silence this warning.",
            type(exc).__name__,
            fallback_url,
        )
        return create_engine(fallback_url, future=True)
    return engine


def _is_local_postgres_url(url: str) -> bool:
    """Whether ``url`` names a Postgres server on this machine."""
    parsed = make_url(url)
    return parsed.drivername.startswith("postgresql") and parsed.host in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def init_schema() -> None:
    """Bring the schema up to date. Cheap and idempotent — safe to invoke
    on every poller/orchestrator start-up.

    SQLite (local development and tests) uses ``create_all`` for tables and
    an additive column upgrade for existing databases. Postgres runs the
    versioned Alembic migrations up to ``head``; the classified-at helper is
    retained for deployments that pre-date Alembic entirely.
    """
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
        _ensure_sqlite_columns(engine)
    else:
        _upgrade_to_head(engine)
        _ensure_classified_at_column(engine)


def reset_sqlite_state() -> bool:
    """Discard the local SQLite database used by the current process.

    ``init`` uses this to start a new local detection run without stale
    feed events or classified records from another repository.  Postgres is
    deliberately never reset here: it may be a shared deployment database.
    Returns whether a SQLite state store was reset.
    """
    global _engine, _session_factory
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return False

    database = engine.url.database
    if database in (None, ":memory:"):
        Base.metadata.drop_all(engine)
    else:
        database_path = Path(database)
        engine.dispose()
        database_path.unlink(missing_ok=True)
    _engine = None
    _session_factory = None
    return True


def _upgrade_to_head(engine: Engine) -> None:
    """Run ``depfix.migrations`` up to ``head`` against a Postgres database.

    Builds an in-memory Alembic ``Config`` rather than reading
    ``alembic.ini`` from disk — the orchestrator may be running from
    inside the Docker image, which only ships ``src/`` (see the
    Dockerfile), not the repo root. ``script_location`` is resolved
    relative to this file instead, so it works the same way whether this
    runs from a checked-out repo or an installed package. ``env.py``
    itself ignores whatever URL (if any) ends up on this ``Config`` and
    always reads ``Settings.database_url`` directly, so there's no second
    "which database" value to keep in sync.
    """
    from alembic import command
    from alembic.config import Config

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    legacy_revision = _legacy_schema_revision(engine)
    if legacy_revision is not None:
        logger.warning(
            "detected pre-Alembic depfix schema; stamping revision %s before upgrade",
            legacy_revision,
        )
        command.stamp(cfg, legacy_revision)
    logger.info("running Alembic migrations to head against %s", engine.url.render_as_string())
    command.upgrade(cfg, "head")


def _legacy_schema_revision(engine: Engine) -> str | None:
    """Return the revision represented by a complete pre-Alembic schema.

    ``None`` means either a fresh database (which Alembic should migrate from
    scratch) or one which is already versioned.  A partially-created legacy
    schema is unsafe to stamp: it requires operator review rather than risking
    data loss or claiming migrations that were never applied.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return None

    present_baseline_tables = tables & _BASELINE_TABLES
    if not present_baseline_tables:
        return None

    missing_baseline_tables = _BASELINE_TABLES - tables
    if missing_baseline_tables:
        missing = ", ".join(sorted(missing_baseline_tables))
        raise RuntimeError(
            "Detected a partial pre-Alembic depfix schema. Refusing to stamp it; "
            f"missing baseline tables: {missing}. Restore the schema or migrate it manually."
        )

    if "change_attempt" not in tables:
        return "0001"

    change_event_columns = {column["name"] for column in inspector.get_columns("change_event")}
    if "body_url" not in change_event_columns:
        return "0002"
    fix_run_columns = {column["name"] for column in inspector.get_columns("fix_run")}
    if "confidence_tier" not in fix_run_columns:
        return "0003"
    if "escalated" not in fix_run_columns:
        return "0004"
    pr_columns = {column["name"] for column in inspector.get_columns("pull_request")}
    if "state" not in pr_columns:
        return "0005"
    breaking_change_columns = {
        column["name"] for column in inspector.get_columns("breaking_change")
    }
    if "severity" not in breaking_change_columns:
        return "0006"
    if "repo_scan_change" not in tables:
        return "0007"
    fix_run_columns = {column["name"] for column in inspector.get_columns("fix_run")}
    if "cost_classify" not in fix_run_columns:
        return "0008"
    repo_scan_columns = {column["name"] for column in inspector.get_columns("repo_scan")}
    return "0010" if "dependencies" in repo_scan_columns else "0009"


def _ensure_classified_at_column(engine: Engine) -> None:
    """Defensively repair only very old Postgres schemas."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE change_event ADD COLUMN IF NOT EXISTS classified_at TIMESTAMPTZ"
        )


def _ensure_sqlite_columns(engine: Engine) -> None:
    """Add migration columns to an existing SQLite database.

    ``create_all`` does not alter tables that already exist. This idempotent
    additive upgrade preserves existing rows and mirrors migrations 0002-0007
    for local SQLite databases.
    """
    existing_tables = set(inspect(engine).get_table_names())
    additions: dict[str, dict[str, str]] = {
        "change_event": {"classified_at": "DATETIME", "body_url": "TEXT"},
        "fix_run": {
            "confidence_tier": "VARCHAR(16) NOT NULL DEFAULT 'none'",
            "typechecked": "BOOLEAN NOT NULL DEFAULT 0",
            "escalated": "BOOLEAN NOT NULL DEFAULT 0",
            "cost_classify": "REAL NOT NULL DEFAULT 0",
            "cost_characterization": "REAL NOT NULL DEFAULT 0",
            "cost_call_site_judge": "REAL NOT NULL DEFAULT 0",
        },
        "file_fix": {
            "origin": "VARCHAR(16) NOT NULL DEFAULT 'llm'",
            "codemod_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        },
        "pull_request": {
            "state": "VARCHAR(16) NOT NULL DEFAULT 'open'",
            "merged": "BOOLEAN NOT NULL DEFAULT 0",
            "closed_at": "DATETIME",
            "merged_at": "DATETIME",
            "last_reconciled_at": "DATETIME",
        },
        "breaking_change": {
            "severity": "VARCHAR(16)",
            "severity_evidence": "VARCHAR(32)",
        },
        "call_site": {
            "severity": "VARCHAR(16)",
            "severity_evidence": "VARCHAR(32)",
            "context_before": "TEXT NOT NULL DEFAULT '[]'",
            "context_after": "TEXT NOT NULL DEFAULT '[]'",
        },
        "repo_scan": {
            "dependencies": "TEXT NOT NULL DEFAULT '[]'",
            "narrowed_symbols": "TEXT NOT NULL DEFAULT '[]'",
        },
    }

    with engine.begin() as connection:
        for table, columns in additions.items():
            if table not in existing_tables:
                continue
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    get_engine()  # ensure factory is built
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
