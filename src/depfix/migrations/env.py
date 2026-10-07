"""Alembic environment script.

The one non-obvious thing here: ``sqlalchemy.url`` is *never* read from
``alembic.ini`` (or from whatever ``-x`` args a caller passes) -- it always
comes from :func:`depfix.config.get_settings`, the same source
``storage/db.get_engine()`` uses. That keeps ``Settings.database_url`` the
single source of truth for "which database", instead of a second config
value that could silently drift from the one the app itself connects to.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from depfix.config import get_settings
from depfix.storage.schema import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
