"""Tests for the simplified project Config screen."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.screen import Screen

from aim.core import declarations, layout_profiles, templates
from aim.core import init as init_mod
from aim.tui.app import AimApp
from aim.tui.screens.config_screen import ConfigScreen


@pytest.mark.asyncio
async def test_project_tab_shows_current_manifest(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfigScreen(project_root))
        await pilot.pause()
        from textual.widgets import Input, Static

        assert app.screen.query_one("#proj-root", Input).value == str(project_root.resolve())
        assert app.screen.query_one("#proj-template", Input).value == "default"
        # Agent dialect is managed by the active layout profile, not the Config screen.
        assert not app.screen.query("#proj-dialect")
        # Active layout profile summary is shown.
        assert (
            "layout profile" in str(app.screen.query_one("#active-profile", Static).content).lower()
        )


@pytest.mark.asyncio
async def test_project_save_writes_manifest(home: Path, project_root: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfigScreen(project_root))
        await pilot.pause()
        from textual.widgets import Button

        for btn in app.screen.query(Button):
            if btn.id == "proj-save":
                btn.press()
                break
        await pilot.pause()
        await pilot.pause()

    decl = declarations.load(project_root)
    profile = layout_profiles.resolve_active(project_root)
    for symlink in profile.symlinks:
        assert symlink in decl.symlinks
    # init now writes aim.toml only; the lockfile is produced by `aim lock`.
    lock_path = project_root / "aim.lock.toml"
    assert not lock_path.exists()


@pytest.mark.asyncio
async def test_project_save_updates_instruction_template(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    custom_template = tmp_path / "custom.md.j2"
    custom_template.write_text("# custom scaffold\n")
    templates.register_user_template("custom", custom_template, description="test template")

    init_mod.run(init_mod.InitOptions(project_root=project_root))
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfigScreen(project_root))
        await pilot.pause()
        from textual.widgets import Button, Input

        app.screen.query_one("#proj-template", Input).value = "custom"
        for btn in app.screen.query(Button):
            if btn.id == "proj-save":
                btn.press()
                break
        await pilot.pause()
        await pilot.pause()

    decl = declarations.load(project_root)
    assert decl.instruction_template == "custom"


@pytest.mark.asyncio
async def test_active_profile_refreshes_on_resume(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfigScreen(project_root))
        await pilot.pause()
        from textual.widgets import Static

        active_label = app.screen.query_one("#active-profile", Static)
        initial = str(active_label.content)
        assert "claude" in initial.lower()

        custom = layout_profiles.BUILTIN_CLAUDE.model_copy(
            update={"name": "custom-test", "scope": layout_profiles.LayoutProfileScope.GLOBAL}
        )
        layout_profiles.save_global_profile(project_root, custom)
        layout_profiles.set_active(project_root, "custom-test")

        # Push and pop a dummy screen so ConfigScreen's on_screen_resume fires.
        app.push_screen(Screen())
        await pilot.pause()
        app.pop_screen()
        await pilot.pause()

        updated = str(app.screen.query_one("#active-profile", Static).content)
        assert "custom-test" in updated
        assert updated != initial
