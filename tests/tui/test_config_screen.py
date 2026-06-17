"""Tests for the simplified project Config screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import declarations, layout_profiles
from aim.core import init as init_mod
from aim.tui.app import AimApp
from aim.tui.screens.config_screen import ConfigScreen


@pytest.mark.asyncio
async def test_project_tab_shows_current_manifest(home: Path, project_root: Path) -> None:
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            agent_dialect="claude",
        )
    )
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
    lock_path = project_root / "aim.lock"
    assert not lock_path.exists()
