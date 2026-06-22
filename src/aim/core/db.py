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
from sqlmodel import Session, create_engine

from aim.core import paths
from aim.core.models import (  # noqa: F401
    AgentIndex,
    ArchetypeIndex,
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

# Alembic migration scripts ship inside the package; built into a Config at runtime.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Head revision id, kept in sync with the latest script in migrations/versions/.
# The cheap at-head check below compares the DB's recorded revision against this to
# avoid importing Alembic on every launch. Bump it when adding an Alembic revision;
# the `test_head_revision_matches_script_head` drift guard enforces the match.
HEAD_REVISION = "b7e1c0a4d2f3"


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
        _run_migrations(engine)
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
    # 30s matches the DBAPI connect timeout: a writer blocked by another process
    # (e.g. a second aim/TUI instance) waits instead of failing immediately.
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _alembic_config(connection: object) -> object:
    """Build a programmatic Alembic Config bound to a live connection.

    Avoids any on-disk `alembic.ini` at runtime (the repo's ini is dev-time only).
    The connection is injected so migrations reuse the WAL/busy-timeout engine.

    Args:
        connection: The SQLAlchemy connection migrations should run on.

    Returns:
        A configured Alembic `Config`.
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def _current_revision(connection: object) -> str | None:
    """Return the DB's recorded Alembic revision, or None if unmanaged.

    A cheap raw-SQL read of `alembic_version` that imports no Alembic machinery, so the
    common already-at-head launch can skip Alembic entirely. A missing table or empty
    row (a fresh database) returns None, routing the caller to the full upgrade.

    Args:
        connection: A live SQLAlchemy connection.

    Returns:
        The single `version_num` value, or None when the version table is absent/empty.
    """
    from sqlalchemy.exc import DatabaseError

    try:
        row = connection.exec_driver_sql(  # type: ignore[attr-defined]
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    except DatabaseError:
        # No alembic_version table yet (fresh DB) — needs the full upgrade.
        return None
    return row[0] if row else None


def _run_migrations(engine: Engine) -> None:
    """Bring the database schema to head via Alembic, skipping the work when already at head.

    The database is built and migrated entirely by Alembic: a fresh database runs the
    full revision chain (creating every table), and a managed one applies only the new
    revisions. This is a greenfield project — there are no pre-Alembic databases to
    adopt, so no reconcile/bridge path is needed.

    Importing Alembic (and the Mako engine it pulls in) and running its environment is
    fixed per-launch overhead. A cheap raw-SQL check of the recorded revision lets the
    common case (DB already at `HEAD_REVISION`) return without importing Alembic at all.

    Args:
        engine: The live, WAL-configured engine.
    """
    with engine.connect() as connection:
        if _current_revision(connection) == HEAD_REVISION:
            return
    # A fresh-DB check raises and poisons its transaction, so the upgrade runs on its
    # own clean connection (also restoring the original single-connection upgrade path).
    from alembic import command

    with engine.connect() as connection:
        cfg = _alembic_config(connection)
        command.upgrade(cfg, "head")


def reset_engine() -> None:
    """Dispose and clear the cached engine between tmp_path test fixtures."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def is_locked_error(exc: BaseException) -> bool:
    """Return whether an exception is a SQLite "database is locked" failure."""
    return "database is locked" in str(exc).lower()


def unlock() -> list[str]:
    """Recover a wedged database by checkpointing the write-ahead log.

    Drops this process's pooled connections, then opens a dedicated short-lived
    connection (bypassing the engine pool) and runs a truncating WAL checkpoint to
    fold the `-wal` file back into the main database and release WAL state. Safe to
    run anytime — it never force-deletes sidecar files, which could corrupt a database
    another process is using. A checkpoint that stays busy means another live process
    holds the database; the failsafe reports that instead of pretending it unlocked.

    Returns:
        Human-readable descriptions of the actions taken, for the CLI to print.
    """
    import sqlite3

    path = paths.db_path()
    if not path.exists():
        return [f"no database at {path}; nothing to unlock"]
    actions = ["closed this process's database connections"]
    reset_engine()
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        actions.append(f"checkpoint failed ({exc}); another process is holding the database")
        return actions
    if row and row[0] == 1:  # (busy, log_pages, checkpointed_pages)
        actions.append("checkpoint is blocked — another process is holding the database")
    else:
        actions.append("checkpointed the write-ahead log; the database is unlocked")
    return actions


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
