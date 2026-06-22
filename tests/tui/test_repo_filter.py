"""TUI test: the skills screen's `f` opens a repo-filter picker (not a cycle)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList

from aim.core import repos
from aim.tui.app import AimApp
from aim.tui.modals.repo_filter import RepoFilterModal
from aim.tui.screens.skills_screen import SkillsScreen
from tests.fixtures import git_fixtures


def _add_skill_repo(tmp_path: Path, alias: str) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / alias, files={f"skills/{alias}skill/SKILL.md": f"# {alias}\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / f"{alias}.git")
    repos.add(alias, f"file://{bare}")


@pytest.mark.asyncio
async def test_skills_repo_filter_picks_one_repo(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _add_skill_repo(tmp_path, "alpha")
    _add_skill_repo(tmp_path, "beta")

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app.push_screen(SkillsScreen())
        await pilot.pause()

        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, RepoFilterModal)

        # Options are: [All repos, alpha, beta]; pick "beta".
        app.screen.query_one(OptionList).highlighted = 2
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, SkillsScreen)
        assert app.screen._repo_filter == "beta"

        # Re-open and choose "All repos" to clear the filter.
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one(OptionList).highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen._repo_filter is None
