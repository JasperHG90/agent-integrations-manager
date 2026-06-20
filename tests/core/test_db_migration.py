"""Tests for Alembic adoption + the pre-Alembic column bridge in `db.py`.

Repros the user-hit crash: an existing SQLite DB created before the
`prereqs`/`provides` columns were added must still work after upgrade, and a
database created before Alembic existed must be bridged + stamped rather than
re-created.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel, select

from aim.core import db
from aim.core.models import SkillIndex


def _fabricate_pre_alembic_db(db_path: Path) -> None:
    """Build a DB the old create_all code would have made: full schema, no Alembic
    version row, and a skillindex predating the prereqs/provides columns."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(raw)
    with raw.begin() as conn:
        # A genuine pre-Alembic DB predates tables added in later Alembic revisions
        # (e.g. archetypeindex from 0002) — drop it so the bridge + upgrade recreate it.
        conn.exec_driver_sql("DROP TABLE archetypeindex")
        conn.exec_driver_sql("ALTER TABLE skillindex DROP COLUMN prereqs")
        conn.exec_driver_sql("ALTER TABLE skillindex DROP COLUMN provides")
        conn.exec_driver_sql(
            "INSERT INTO skillindex "
            "(qualified_name, repo_alias, skill_name, source_path, indexed_at_sha) "
            "VALUES ('legacy/foo', 'legacy', 'foo', 'skills/foo', 'abc123')"
        )
    raw.dispose()


def test_pre_alembic_db_is_bridged_and_stamped(home: Path) -> None:
    _fabricate_pre_alembic_db(home / "data" / "aim.sqlite")

    db.reset_engine()
    db.get_engine()  # legacy path: bridge missing columns + stamp baseline.

    with db.session() as s:
        rows = list(s.exec(select(SkillIndex)).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.qualified_name == "legacy/foo"
    # Bridged columns now exist with their defaults.
    assert row.prereqs == ""
    assert row.provides == ""

    # The DB is now under Alembic management and brought up to head: it was stamped at
    # baseline, then later revisions applied (e.g. the archetype table from 0002).
    with db.session() as s:
        live = inspect(s.connection())
        assert live.has_table("alembic_version")
        assert live.has_table("archetypeindex")
        version = s.exec(text("SELECT version_num FROM alembic_version")).one()
    assert version[0] is not None


def test_fresh_db_is_created_at_head(home: Path) -> None:
    db.reset_engine()
    db.get_engine()  # fresh path: upgrade head creates every table.
    with db.session() as s:
        live = inspect(s.connection())
        assert live.has_table("skillindex")
        assert live.has_table("registeredrepo")
        assert live.has_table("alembic_version")


def test_migration_is_idempotent(home: Path) -> None:
    """Running get_engine repeatedly must not fail (upgrade head is idempotent)."""
    db.get_engine()
    db.reset_engine()
    db.get_engine()
