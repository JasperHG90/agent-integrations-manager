from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import repos
from aim.tui.app import AimApp
from aim.tui.modals.palette import PaletteModal
from tests.fixtures import git_fixtures


@pytest.mark.asyncio
async def test_palette_opens(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_palette()
        await pilot.pause()
        assert isinstance(app.screen, PaletteModal)


@pytest.mark.asyncio
async def test_palette_filters_by_substring(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/code-review/SKILL.md": "# code-review\n",
            "rules/be-concise.md": "Be concise.\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_palette()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, PaletteModal)

        from textual.widgets import Input, OptionList

        search = modal.query_one("#palette-input", Input)
        search.value = "concise"
        await pilot.pause()
        olist = modal.query_one(OptionList)
        assert olist.option_count == 1


@pytest.mark.asyncio
async def test_palette_action_routes_to_screen(home: Path) -> None:
    from aim.tui.screens.repos_screen import ReposScreen

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_palette()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, PaletteModal)

        from textual.widgets import Input

        modal.query_one("#palette-input", Input).value = "Open Repos"
        await pilot.pause()
        modal.action_activate()
        await pilot.pause()
        assert isinstance(app.screen, ReposScreen)
