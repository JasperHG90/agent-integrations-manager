"""Alembic environment for aim's SQLite cache database.

At runtime, `db.py` passes the live engine connection via
`config.attributes["connection"]` so migrations reuse the WAL/busy-timeout
configured engine. For dev-time autogeneration (the `alembic` CLI), a URL from
`alembic.ini` is used instead.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import aim.core.models  # noqa: F401  -- import populates SQLModel.metadata

config = context.config
target_metadata = SQLModel.metadata


def _run(connection: object) -> None:
    """Configure the migration context for a live connection and run it.

    Args:
        connection: An open SQLAlchemy connection to migrate against.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite needs batch mode for ALTER operations.
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the connection injected by db.py, or a new engine."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as conn:
        _run(conn)


def run_migrations_offline() -> None:
    """Emit SQL for migrations without a live DB connection (dev-time only)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
