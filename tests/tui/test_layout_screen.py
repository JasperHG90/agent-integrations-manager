"""TUI tests for the combined layout screen (profiles + plugin targets tabs)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, Static, TabbedContent

from aim.core import layout_profiles, manifest, repos
from aim.tui.app import AimApp
from aim.tui.modals.busy import BusyModal
from aim.tui.modals.layout_profile_modal import LayoutProfileModal
from aim.tui.modals.target_install import TargetInstallConfig
from aim.tui.screens.layout_screen import LayoutScreen
from tests.fixtures import git_fixtures

_TARGET = """
name = "opencode"
[manifest]
file = "package.json"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def _register_target_repo(tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"targets/opencode.toml": _TARGET, "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")


@pytest.mark.asyncio
async def test_layout_screen_lists_builtin_profiles(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LayoutScreen)
        table = app.screen.query_one("#profiles-table", DataTable)
        names = {table.get_row_at(row)[1] for row in range(table.row_count)}
        assert "Claude Code" in names
        assert "Gemini CLI" in names


@pytest.mark.asyncio
async def test_layout_screen_adds_project_profile(home: Path, project_root: Path) -> None:
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
async def test_layout_screen_sets_active_profile(home: Path, project_root: Path) -> None:
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


@pytest.mark.asyncio
async def test_layout_screen_lists_targets_in_tab(home: Path, tmp_path: Path) -> None:
    _register_target_repo(tmp_path)
    # An isolated project root: with cwd (the aim repo itself) the project's own
    # .aim/targets/*.toml would legitimately add active rows to the tab.
    project = tmp_path / "proj"
    project.mkdir()
    app = AimApp(project_root=project)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LayoutScreen)
        table = screen.query_one("#targets-table", DataTable)
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "a/opencode"
        # Profile actions are guarded while the Targets tab is active.
        screen.query_one(TabbedContent).active = "targets"
        await pilot.pause()
        screen.action_add_profile()
        assert isinstance(app.screen, LayoutScreen)  # no modal pushed


@pytest.mark.asyncio
async def test_edit_profile_modal_shows_target_settings(home: Path, project_root: Path) -> None:
    """The profile edit modal surfaces the project's plugin targets read-only —
    where plugins land is target-owned since the layout/targets fold, and the
    edit view must show the full layout picture."""
    targets_dir = project_root / ".aim" / "targets"
    targets_dir.mkdir(parents=True)
    (targets_dir / "opencode.toml").write_text(_TARGET)

    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("e")  # edit the selected (built-in) profile
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, LayoutProfileModal)
        info = str(modal.query_one("#targets-info", Static).render())
        assert "opencode: plugins → .opencode/plugins/{name}" in info


@pytest.mark.asyncio
async def test_edit_profile_modal_without_targets_points_at_targets_tab(
    home: Path, project_root: Path
) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("a")  # the add modal carries the same section
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, LayoutProfileModal)
        info = str(modal.query_one("#targets-info", Static).render())
        assert "none — install targets on the Targets tab" in info


@pytest.mark.asyncio
async def test_layout_screen_slash_focuses_search_bar(home: Path, tmp_path: Path) -> None:
    """Pressing / from the Profiles tab lands focus in the search bar, so the
    next keystrokes type a query instead of firing action keys (regression:
    the queued TabActivated handler used to steal focus back to the table)."""
    _register_target_repo(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LayoutScreen)
        await pilot.press("slash")
        await pilot.pause()
        focused = app.focused
        assert focused is not None and focused.id == "search-bar"
        # Typing an action key now types into the search bar — no modal opens.
        await pilot.press("i")
        await pilot.pause()
        assert app.screen is screen
        assert screen.query_one("#search-bar", Input).value == "i"
        # Pressing / while already on the Targets tab also focuses the bar.
        screen.query_one("#targets-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        focused = app.focused
        assert focused is not None and focused.id == "search-bar"


@pytest.mark.asyncio
async def test_layout_screen_install_clears_overlay(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _register_target_repo(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = LayoutScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._install("a/opencode", TargetInstallConfig(project_root=project_root))
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The overlay is dismissed once the worker finishes.
        assert not isinstance(app.screen, BusyModal)
        assert screen._busy is None

    assert (project_root / ".aim" / "targets" / "opencode.toml").exists()


@pytest.mark.asyncio
async def test_layout_profile_modal_invalid_name_shows_error_without_crashing(
    home: Path, project_root: Path
) -> None:
    """Issue #2: the Pydantic error's `[type=..., input_value=...]` must not be parsed as markup."""
    app = AimApp(project_root=project_root)
    # notifications=True so the error toast actually renders (and parses its message).
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, LayoutProfileModal)
        modal.query_one("#name", Input).value = "Opencode"
        await pilot.pause()
        modal.query_one("#save", Button).press()
        await pilot.pause()
        await pilot.pause()

        assert app.screen is modal
        assert "invalid profile" in str(modal.query_one("#error", Static).render())
