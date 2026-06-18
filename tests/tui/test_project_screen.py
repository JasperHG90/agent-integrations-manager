from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import init, install, repos, rule_install
from aim.core import sync as sync_mod
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run
from aim.tui.app import AimApp
from aim.tui.screens.project_screen import ProjectScreen
from tests.fixtures import git_fixtures


@pytest.mark.asyncio
async def test_project_screen_empty_when_no_manifest(home: Path, project_root: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ProjectScreen(project_root)
        app.push_screen(screen)
        await pilot.pause()
        assert "no aim.lock.toml" in screen.last_status


@pytest.mark.asyncio
async def test_project_screen_shows_clean_and_edited(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectScreen(project_root))
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one(DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        # columns: skill, version, target, drift
        assert row[0] == "a/foo"
        assert row[3] == "clean"

    # Now edit the file and re-open.
    (project_root / ".claude" / "skills" / "foo" / "SKILL.md").write_text("hand-edit\n")
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectScreen(project_root))
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one(DataTable)
        assert table.get_row_at(0)[3] == "edited"


@pytest.mark.asyncio
async def test_project_screen_rules_tab(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "rsrc", files={"rules/be-concise.md": "Be concise.\n", "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "rbare.git")
    init.run(init.InitOptions(project_root=project_root))
    repos.add("rr", f"file://{bare}")
    rule_install.install(project_root, "rr/be-concise")
    await lock_run(LockOptions(project_root=project_root))
    await sync_mod.run(sync_mod.SyncOptions(project_root=project_root))

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectScreen(project_root))
        await pilot.pause()
        await pilot.press("tab", "tab", "tab")
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one("#rules-table", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "rr/be-concise"
        assert row[2] == "clean"
