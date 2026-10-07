"""Smoke tests for the TUI via Textual's Pilot harness.

Snapshot scenarios are enumerated in `tests/tui/snapshots/README.md`:
- main screen (always populated identically)
- repos screen: empty vs. with one repo
- skills screen: empty vs. with skills vs. with active search filter
- rules screen: empty vs. with rules
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import repos
from aim.tui.app import AimApp
from tests.fixtures import git_fixtures


def _register_rule_repo(tmp_path: Path, names: list[str]) -> None:
    files = {f"rules/{n}.md": f"{n} body\n" for n in names}
    files["README.md"] = "x\n"
    working = git_fixtures.make_source_repo(tmp_path / "rsrc", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "rbare.git")
    repos.add("rr", f"file://{bare}")


@pytest.mark.asyncio
async def test_main_screen_navigates_to_repos_and_back(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"
        await pilot.press("r")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ReposScreen"
        await pilot.press("b")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


@pytest.mark.asyncio
async def test_main_screen_navigates_to_skills(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "SkillsScreen"


@pytest.mark.asyncio
async def test_repos_screen_shows_registered_repo(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_skills_screen_search_filters(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/review/SKILL.md": "# Review\n",
            "skills/format/SKILL.md": "# Format\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        from textual.widgets import DataTable, Input

        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        search = app.screen.query_one("#search-bar", Input)
        search.value = "review"
        await pilot.pause()
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_sync_profiles_runs_in_background_and_surfaces_warnings(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main screen must paint without waiting on profile sync, and sync warnings
    must still reach the user via a notification from the background worker."""
    from aim.core import layout_profiles

    report = layout_profiles.SyncReport(warnings=["stale profile foo"])
    monkeypatch.setattr(layout_profiles, "sync_profiles", lambda root: report)

    notifications: list[str] = []
    app = AimApp()
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))

    async with app.run_test() as pilot:
        await pilot.pause()
        # Screen is up immediately — not blocked on sync_profiles.
        assert app.screen.__class__.__name__ == "MainScreen"
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "stale profile foo" in notifications


@pytest.mark.asyncio
async def test_rules_screen_lists_rules(home: Path, tmp_path: Path) -> None:
    _register_rule_repo(tmp_path, ["be-concise", "test-first"])
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.screen.query_one(DataTable)
        assert table.row_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "x [type=value_error, input_value='Opencode', input_type=str]",
        "add a repo first (press [a])",
        "[/] unbalanced",
    ],
)
async def test_notify_treats_message_as_plain_text(home: Path, message: str) -> None:
    """Issue #2: bracketed error text must render literally, not be parsed as markup."""
    # Toast is private, but it is the widget that parses the message; no public alias.
    from textual.widgets._toast import Toast

    app = AimApp()
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        app.notify(message, severity="error")
        await pilot.pause()
        await pilot.pause()
        toasts = list(app.screen.query(Toast))
        assert toasts
        assert message in str(toasts[-1].render())
