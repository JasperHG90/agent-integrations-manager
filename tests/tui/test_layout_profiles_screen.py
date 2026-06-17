"""TUI tests for the layout profiles screen."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from aim.core import layout_profiles, manifest
from aim.tui.app import AimApp
from aim.tui.modals.layout_profile_modal import LayoutProfileModal
from aim.tui.screens.layout_profiles_screen import LayoutProfilesScreen


@pytest.mark.asyncio
async def test_layout_profiles_screen_lists_builtins(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LayoutProfilesScreen)
        table = app.screen.query_one("#profiles-table", DataTable)
        names = {table.get_row_at(row)[1] for row in range(table.row_count)}
        assert "Claude Code" in names
        assert "Gemini CLI" in names


@pytest.mark.asyncio
async def test_layout_profiles_screen_adds_project_profile(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, LayoutProfileModal)
        modal.query_one("#name", Input).value = "custom"
        modal.query_one("#skills-dir", Input).value = ".custom/skills"
        modal.query_one("#symlinks", Input).value = "CUSTOM.md"
        await pilot.pause()
        from textual.widgets import Button

        for btn in modal.query(Button):
            if btn.id == "save":
                btn.press()
                break
        await pilot.pause()
        await pilot.pause()

    profile = layout_profiles.get_profile(project_root, "custom")
    assert profile.skills_dir == ".custom/skills"
    assert profile.symlinks == ["CUSTOM.md"]
    assert profile.scope == layout_profiles.LayoutProfileScope.PROJECT


@pytest.mark.asyncio
async def test_layout_profiles_screen_sets_active(home: Path, project_root: Path) -> None:
    layout_profiles.save_project_profile(
        project_root,
        layout_profiles.LayoutProfile(name="custom", skills_dir=".custom/skills"),
    )
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        # Built-ins come first (claude, gemini), then the custom project profile.
        await pilot.press("down", "down")
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

    m = manifest.load(project_root)
    assert m.layout_profile == "custom"
