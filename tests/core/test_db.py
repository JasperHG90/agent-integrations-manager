from __future__ import annotations

from pathlib import Path

from sqlmodel import select

from aim.core import db, policy
from aim.core.models import RegisteredRepo


def test_engine_creates_tables(home: Path) -> None:
    engine = db.get_engine()
    assert engine is not None
    with db.session() as s:
        result = s.exec(select(RegisteredRepo)).all()
        assert result == []


def test_round_trip_registered_repo(home: Path) -> None:
    db.get_engine()
    url = "https://github.com/anthropics/skills"
    with db.session() as s:
        s.add(RegisteredRepo(alias="anthropic", repo_id=policy.repo_id_for_url(url), url=url))
        s.commit()
    with db.session() as s:
        rows = s.exec(select(RegisteredRepo)).all()
        assert len(rows) == 1
        assert rows[0].alias == "anthropic"
        assert rows[0].repo_id == policy.repo_id_for_url(url)
