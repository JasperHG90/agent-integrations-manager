"""TUI tests for the startup layout-profile picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_init.core import layout_profiles, manifest
from agent_init.tui.app import AgentInitApp
from agent_init.tui.modals.layout_profile_picker_modal import (
    LayoutProfilePickerModal,
)
from agent_init.tui.screens.main_screen import MainScreen


@pytest.mark.asyncio
async def test_picker_opens_without_global_default(home: Path, project_root: Path) -> None:
    layout_profiles.set_global_default(None)
    app = AgentInitApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LayoutProfilePickerModal)


@pytest.mark.asyncio
async def test_picker_selects_profile(home: Path, project_root: Path) -> None:
    layout_profiles.set_global_default(None)
    app = AgentInitApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LayoutProfilePickerModal)
        # Built-ins come first: claude then gemini.
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    m = manifest.load(project_root)
    assert m.layout_profile == "gemini"
