"""SQLite via SQLModel. The global DB is a cache only; never a source of truth.

The DB stores:
- RegisteredRepo: machine-local registry of skill source repos.
- SkillIndex: cached search index over discovered skills.
- Template: registered AGENTS.md templates (builtin + user).
- RuleEntry: metadata for user-saved rule snippets.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from agent_init.core import paths
from agent_init.core.models import (  # noqa: F401
    AgentIndex,
    GlobalSetting,
    LayoutProfile,
    RegisteredRepo,
    RuleEntry,
    SkillIndex,
    Template,
)

_engine: Engine | None = None


def get_engine(db_path: Path | None = None) -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    if db_path is None:
        paths.ensure_global_dirs()
        db_path = paths.db_path()
    _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(_engine)
    _migrate_schema(_engine)
    return _engine


def _migrate_schema(engine: Engine) -> None:
    """Additive schema migration. `SQLModel.metadata.create_all` only creates
    *missing tables*; it never ALTERs an existing one to add a new column.

    When fields are added to an ORM model the next `agent-init` run against
    an older DB crashes with `no such column: ...`. Walk each model table,
    diff against the live SQLite schema, and `ALTER TABLE ADD COLUMN` for
    anything missing. Safe only for additive changes (new column must be
    nullable or have a default)."""
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
            elif isinstance(default, (int, float)):
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
    """Used by tests to drop the cached engine between tmp_path fixtures."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session() -> Iterator[Session]:
    with Session(get_engine()) as s:
        yield s
