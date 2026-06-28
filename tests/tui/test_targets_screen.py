"""Targets TUI screen: open from the main menu and install with the overlay."""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import repos
from aim.tui.app import AimApp
from aim.tui.modals.busy import BusyModal
from aim.tui.modals.target_install import TargetInstallConfig
from aim.tui.screens.targets_screen import TargetsScreen
from tests.fixtures import git_fixtures

_TARGET = """
name = "opencode"
[manifest]
file = "package.json"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def _register(tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"targets/opencode.toml": _TARGET, "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")


@pytest.mark.asyncio
async def test_main_menu_opens_targets_screen(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TargetsScreen)


@pytest.mark.asyncio
async def test_targets_screen_install_clears_overlay(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _register(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = TargetsScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._install("a/opencode", TargetInstallConfig(project_root=project_root))
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The overlay is dismissed once the worker finishes.
        assert not isinstance(app.screen, BusyModal)
        assert screen._busy is None

    assert (project_root / ".aim" / "targets" / "opencode.toml").exists()
