"""TUI tests for app startup.

The default startup layout is now the built-in Claude profile, so the app opens
MainScreen directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import layout_profiles, manifest
from aim.tui.app import AimApp
from aim.tui.screens.main_screen import MainScreen


@pytest.mark.asyncio
async def test_starts_on_main_screen_with_default_claude_profile(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


@pytest.mark.asyncio
async def test_profile_override_sets_active_profile(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root, profile_name="gemini")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    m = manifest.load(project_root)
    assert m.layout_profile == "gemini"


@pytest.mark.asyncio
async def test_invalid_profile_override_warns_and_uses_default(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root, profile_name="missing")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    m = manifest.load_or_default(project_root)
    assert m.layout_profile is None
    assert layout_profiles.resolve_active(project_root) == layout_profiles.BUILTIN_CLAUDE
