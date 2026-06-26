"""Tests for the Alembic-managed schema in `db.py`.

The database is built and migrated entirely by Alembic (greenfield — no
pre-Alembic databases to adopt).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from aim.core import db


def test_fresh_db_is_created_at_head(home: Path) -> None:
    db.reset_engine()
    db.get_engine()  # upgrade head runs the full revision chain, creating every table.
    with db.session() as s:
        live = inspect(s.connection())
        assert live.has_table("skillindex")
        assert live.has_table("registeredrepo")
        assert live.has_table("archetypeindex")
        assert live.has_table("alembic_version")


def test_migration_is_idempotent(home: Path) -> None:
    """Running get_engine repeatedly must not fail (upgrade head is idempotent)."""
    db.get_engine()
    db.reset_engine()
    db.get_engine()


def test_at_head_launch_skips_alembic_upgrade(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch against an already-at-head DB must not run the Alembic upgrade.

    The fresh build runs the real upgrade once; a subsequent engine build on the same
    (now at-head) database must short-circuit on the cheap revision check and never call
    `command.upgrade` again.
    """
    import alembic.command as alembic_command

    real_upgrade = alembic_command.upgrade
    calls: list[str] = []

    def spy(cfg: object, revision: str, *args: object, **kwargs: object) -> None:
        calls.append(revision)
        real_upgrade(cfg, revision, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alembic_command, "upgrade", spy)

    db.reset_engine()
    db.get_engine()  # fresh DB -> full upgrade runs once
    assert calls == ["head"]

    db.reset_engine()
    db.get_engine()  # already at head -> must not upgrade again
    assert calls == ["head"]


def _repo_id_index_is_unique(db_path: Path, rows: list[tuple[str, str]]) -> bool | None:
    """Build a `registeredrepo` DB at the pre-repo_id revision, seed `rows`, upgrade to
    head, and report whether the resulting `repo_id` index is unique (None if absent).

    Args:
        db_path: Where to create the throwaway SQLite database.
        rows: ``(alias, url)`` pairs to seed before the repo_id migration runs.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            command.upgrade(db._alembic_config(conn), "d4f2a1b6c8e0")  # pre repo_id
        with engine.begin() as conn:
            for alias, url in rows:
                conn.exec_driver_sql(
                    "INSERT INTO registeredrepo (alias, url, default_ref) VALUES (?, ?, 'HEAD')",
                    (alias, url),
                )
        with engine.connect() as conn:
            command.upgrade(db._alembic_config(conn), "head")  # must not raise
        with engine.connect() as conn:
            indexes = [
                ix
                for ix in inspect(conn).get_indexes("registeredrepo")
                if ix["column_names"] == ["repo_id"]
            ]
            return bool(indexes[0]["unique"]) if indexes else None  # SQLite reports 1/0
    finally:
        engine.dispose()


def test_repo_id_index_unique_on_collision_free_legacy_db(home: Path, tmp_path: Path) -> None:
    """A pre-repo_id DB with no duplicate upstream repos backfills cleanly to a UNIQUE
    repo_id index — the universal case for DBs created after the dedup fix."""
    assert _repo_id_index_is_unique(tmp_path / "clean.db", [("r1", "https://github.com/org/one")])


def test_repo_id_index_non_unique_on_duplicate_legacy_db(home: Path, tmp_path: Path) -> None:
    """A pre-repo_id DB with two aliases for ONE upstream repo (two clone-URL forms that
    normalize equal) must upgrade WITHOUT a unique-index abort.

    The old alias-only `add` allowed this; both rows backfill to the same repo_id. A
    unique index would abort the upgrade — and since migrations run on first DB touch,
    the user could not even run `aim repo remove` to recover (deadlock). The adaptive
    index falls back to non-unique; `repos.add`/`get_by_id` enforce identity going forward.
    """
    is_unique = _repo_id_index_is_unique(
        tmp_path / "dup.db",
        [("r1", "https://github.com/org/repo"), ("r2", "https://github.com/org/repo.git")],
    )
    assert is_unique is False  # index exists (not None) but fell back to non-unique


def test_head_revision_matches_script_head(home: Path) -> None:
    """`db.HEAD_REVISION` must equal Alembic's script head, or the cheap check rots.

    This is the one place a test imports Alembic: it pins the constant the hot path
    relies on to the real head so adding a migration without bumping it fails loudly.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(db._MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_current_head() == db.HEAD_REVISION
