"""Repos screen: reindex action re-runs discovery for the selected repo."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from textual.widgets import DataTable

from aim.core import db, repo_rules, repos
from aim.tui.app import AimApp
from aim.tui.modals.repo_edit_ref import RepoEditRefModal, RepoEditRefResult
from aim.tui.screens.repos_screen import ReposScreen
from tests.fixtures import git_fixtures

# Table column order: alias(0), ref(1), behind(2), url(3), head(4), last fetched(5), contains(6)


def _register_repo_tracking_stale_tag(tmp_path: Path) -> None:
    """Register `r` tracking tag `v0`, with `main` one rule ahead (1 behind)."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"rules/base.md": "base\n", "README.md": "x\n"}
    )
    git_fixtures.add_tag(working, "v0")
    git_fixtures.add_commit(working, {"rules/extra.md": "extra\n"}, "add extra rule")
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("r", f"file://{bare}", default_ref="v0")


@pytest.mark.asyncio
async def test_repos_screen_reindex_restores_index(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"rules/a.md": "a\n", "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("r", f"file://{bare}")
    # Drop the index without moving the SHA, so only a forced reindex restores it.
    with db.session() as session:
        session.exec(repos._delete_rule_index("r"))
        session.commit()
    assert repo_rules.list_rules("r") == []

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ReposScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen.action_reindex_current()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The repopulated table reflects the rediscovered rule artifact.
        table = screen.query_one(DataTable)
        assert "rules" in table.get_row_at(0)[6]

    assert {row.rule_name for row in repo_rules.list_rules("r")} == {"a"}


@pytest.mark.asyncio
async def test_repos_screen_shows_ref_and_behind(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ReposScreen()
        app.push_screen(screen)
        await pilot.pause()
        row = screen.query_one(DataTable).get_row_at(0)
        assert row[1] == "v0"  # ref column
        # The behind column fills in from a background worker.
        await app.workers.wait_for_complete()
        await pilot.pause()
        row = screen.query_one(DataTable).get_row_at(0)
        assert row[2] == "1 behind main"  # behind column


@pytest.mark.asyncio
async def test_repos_screen_fills_behind_off_the_ui_thread(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_populate must not compute ref lag inline: the table renders immediately
    with a placeholder, and a worker thread fills the behind column afterwards.
    Pins the fix for the slow repos panel (git subprocesses off the UI thread)."""
    _register_repo_tracking_stale_tag(tmp_path)
    release = threading.Event()
    calls: list[threading.Thread] = []
    real = repos.tracked_ref_lag

    def _blocking_spy(alias: str):  # type: ignore[no-untyped-def]
        calls.append(threading.current_thread())
        release.wait(timeout=10)
        return real(alias)

    monkeypatch.setattr(repos, "tracked_ref_lag", _blocking_spy)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ReposScreen()
        app.push_screen(screen)
        await pilot.pause()
        # The table rendered without waiting on the (still blocked) lag computation.
        row = screen.query_one(DataTable).get_row_at(0)
        assert row[2] == "…"
        # Any lag calls so far came from the worker thread, never the UI thread.
        assert all(t is not threading.main_thread() for t in calls)
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen.query_one(DataTable).get_row_at(0)[2] == "1 behind main"
        assert calls and all(t is not threading.main_thread() for t in calls)


@pytest.mark.asyncio
async def test_repos_screen_edit_ref_repoints(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ReposScreen()
        app.push_screen(screen)
        await pilot.pause()
        # Apply the modal result directly (bypassing the interactive modal).
        screen._on_edit_ref(RepoEditRefResult(alias="r", default_ref="main"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert repos.get("r").default_ref == "main"
        # The pause above repopulated the table, kicking off the ref-lag worker.
        await app.workers.wait_for_complete()
        await pilot.pause()
        row = screen.query_one(DataTable).get_row_at(0)
        assert row[1] == "main"  # ref column updated
        assert row[2] == ""  # no longer behind


@pytest.mark.asyncio
async def test_repos_screen_edit_ref_opens_modal(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ReposScreen())
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, RepoEditRefModal)
