"""TUI tests for the archetypes screen: list, select as base, and menu wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable

from aim.core import declarations, repos
from aim.core import init as init_mod
from aim.tui.app import AimApp
from aim.tui.screens.archetypes_screen import ArchetypesScreen
from tests.fixtures import git_fixtures


def _repo_with_archetype(tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"bases/lean/AGENTS.md": "# Lean base\n\nBe terse.\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("co", f"file://{bare}", allow_empty=True)


@pytest.mark.asyncio
async def test_archetypes_screen_lists_and_selects(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _repo_with_archetype(tmp_path)
    init_mod.run(init_mod.InitOptions(project_root=project_root))

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ArchetypesScreen(project_root=project_root))
        await pilot.pause()

        table = app.screen.query_one(DataTable)
        # The built-in default always heads the list, then repo archetypes.
        assert table.row_count == 2
        assert table.get_row_at(0)[0] == "default"
        # Discovered from a non-canonical `bases/lean/` directory.
        assert table.get_row_at(1)[0] == "co/lean"

        table.move_cursor(row=1)
        await pilot.press("u")  # use co/lean as base
        await app.workers.wait_for_complete()
        await pilot.pause()
        declared = declarations.load(project_root).instruction_archetype
        assert declared is not None and declared.qualified_name == "co/lean"

        # Selecting the built-in default reverts to the bundled scaffold.
        table.move_cursor(row=0)
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert declarations.load(project_root).instruction_archetype is None


@pytest.mark.asyncio
async def test_main_menu_opens_archetypes(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, ArchetypesScreen)
