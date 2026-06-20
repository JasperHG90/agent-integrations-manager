"""SQLite via SQLModel. The global DB is a cache only; never a source of truth.

The DB stores:
- RegisteredRepo: machine-local registry of skill source repos.
- SkillIndex: cached search index over discovered skills.
- Template: registered AGENTS.md templates (builtin + user).
- RuleIndex: cached search index over discovered repo-sourced rules.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from aim.core import paths
from aim.core.models import (  # noqa: F401
    AgentIndex,
    GlobalSetting,
    LayoutProfile,
    McpServerCache,
    RegisteredRepo,
    RuleIndex,
    SkillIndex,
    Template,
)

_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine(db_path: Path | None = None) -> Engine:
    """Return the process-wide SQLite engine, creating it once on first use.

    Thread-safe: `sync` first touches the DB from many worker threads at once, so a
    naive check-then-create would let several threads run `create_all`/migrate/WAL
    setup concurrently and collide with "database is locked". A lock with
    double-checked init guarantees a single engine, fully initialized (and switched
    to WAL) before any concurrent connection opens.

    Args:
        db_path: Override for the database file location; defaults to the global DB
            path when omitted.

    Returns:
        The cached SQLModel engine, with WAL enabled, tables created, and schema
        migrated.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        if db_path is None:
            paths.ensure_global_dirs()
            db_path = paths.db_path()
        # check_same_thread=False lets pooled connections cross threads; the busy
        # timeout lets a blocked writer wait for the lock instead of raising.
        engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(engine, "connect", _set_sqlite_pragmas)
        # Convert to WAL once, here, on a single connection while we still hold the
        # lock — doing it per-connect would race when many threads open at once on a
        # pre-existing rollback-mode DB. WAL is persisted in the file, so later
        # connections inherit it.
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        SQLModel.metadata.create_all(engine)
        _migrate_schema(engine)
        _engine = engine
        return _engine


def _set_sqlite_pragmas(dbapi_conn: object, _record: object) -> None:
    """Set the per-connection SQLite pragmas (busy timeout and synchronous mode).

    These are per-connection and so must be set on every connect. WAL journaling is
    a database-level setting applied once in `get_engine`.

    Args:
        dbapi_conn: The freshly opened DBAPI connection.
        _record: The pool's connection record (unused).
    """
    cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _migrate_schema(engine: Engine) -> None:
    """Apply additive schema migrations to bring the DB up to the model.

    `SQLModel.metadata.create_all` only creates *missing tables*; it never
    ALTERs an existing one to add a new column. When fields are added to an
    ORM model the next `aim` run against an older DB crashes with
    `no such column: ...`. Walk each model table, diff against the live
    SQLite schema, and `ALTER TABLE ADD COLUMN` for anything missing. Safe
    only for additive changes (new column must be nullable or have a default).

    Args:
        engine: The live engine whose database is reconciled with the models.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        live_cols = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in live_cols:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            null_clause = "" if column.nullable else " NOT NULL"
            default_clause = ""
            default = getattr(column.default, "arg", None)
            if isinstance(default, bool):
                default_clause = f" DEFAULT {1 if default else 0}"
            elif isinstance(default, int | float):
                default_clause = f" DEFAULT {default}"
            elif isinstance(default, str):
                escaped = default.replace("'", "''")
                default_clause = f" DEFAULT '{escaped}'"
            elif column.nullable:
                default_clause = " DEFAULT NULL"
            stmt = (
                f"ALTER TABLE {table.name} ADD COLUMN "
                f"{column.name} {col_type}{default_clause}{null_clause}"
            )
            with engine.begin() as conn:
                conn.execute(text(stmt))


def reset_engine() -> None:
    """Dispose and clear the cached engine between tmp_path test fixtures."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


_session_lock = threading.RLock()


@contextmanager
def session() -> Iterator[Session]:
    """Yield a database session bound to the shared engine, serialized in-process.

    SQLite allows only one writer, and pysqlite's deferred transactions can deadlock
    on a read->write upgrade (two pooled connections each hold a read lock and both
    try to upgrade) — a "database is locked" that `busy_timeout` cannot resolve. `sync`
    triggers exactly this by indexing many repos from worker threads at once. A
    process-wide reentrant lock serializes sessions so writes never contend; network
    work happens outside sessions, so concurrency there is unaffected.

    Yields:
        An open SQLModel session that is closed on context exit.
    """
    with _session_lock, Session(get_engine()) as s:
        yield s
