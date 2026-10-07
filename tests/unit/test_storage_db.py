"""Tests for safe Postgres schema bootstrapping."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from depfix.storage import db


def _engine_with_tables(
    monkeypatch: pytest.MonkeyPatch,
    tables: list[str],
    *,
    change_event_columns: list[str] | None = None,
) -> MagicMock:
    inspector = MagicMock()
    inspector.get_table_names.return_value = tables
    inspector.get_columns.return_value = [{"name": name} for name in (change_event_columns or [])]
    monkeypatch.setattr(db, "inspect", lambda _engine: inspector)
    return MagicMock()


def test_legacy_schema_is_stamped_at_baseline_before_later_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine_with_tables(monkeypatch, sorted(db._BASELINE_TABLES))

    assert db._legacy_schema_revision(engine) == "0001"


def test_fully_current_legacy_schema_is_stamped_at_current_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine_with_tables(
        monkeypatch,
        sorted({*db._BASELINE_TABLES, "change_attempt"}),
        change_event_columns=["id", "body_url"],
    )

    assert db._legacy_schema_revision(engine) == "0003"


def test_partial_legacy_schema_is_not_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine_with_tables(monkeypatch, ["provider"])

    with pytest.raises(RuntimeError, match="partial pre-Alembic"):
        db._legacy_schema_revision(engine)


def test_sqlite_column_upgrade_is_idempotent_and_preserves_rows() -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE breaking_change (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO breaking_change (id) VALUES (1)"))
        connection.execute(text("CREATE TABLE call_site (id INTEGER PRIMARY KEY)"))

    db._ensure_sqlite_columns(engine)
    db._ensure_sqlite_columns(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("breaking_change")}
    assert {"severity", "severity_evidence"} <= columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM breaking_change")).scalar_one() == 1


def test_reset_sqlite_state_drops_in_memory_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stale_state (id INTEGER PRIMARY KEY)"))

    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_session_factory", None)

    assert db.reset_sqlite_state() is True
    fresh = db.get_engine()
    assert "stale_state" not in inspect(fresh).get_table_names()
