"""TUI verification — mix of structural assertions and a small bitmap-snapshot
backstop.

We learned the hard way that bitmap snapshots are brittle while a theme is
still iterating: every palette tweak invalidates them and trains us to
`--snapshot-update` reflexively. So the bulk of coverage is now structural
(widget IDs exist, tables have expected row counts, titles contain expected
strings). A *single* bitmap test per screen remains as a layout-regression
backstop.
"""

from __future__ import annotations

import os
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


def _setup_repo_with_skills(tmp_path: Path, files: dict[str, str]) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")


# ---------- Structural: cheap, robust to theme changes ----------


@pytest.mark.asyncio
async def test_main_screen_structure(home: Path) -> None:
    from textual.widgets import Static

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.screen.query_one("#banner", Static)
        # The ASCII banner contains block glyphs spelling AGENT INIT — checking
        # for one of those characters is enough to detect it's been rendered.
        rendered = str(banner.render())
        assert "█" in rendered
        # Version and profile/path metadata are rendered next to the rocket.
        from aim import __version__

        assert __version__ in rendered


@pytest.mark.asyncio
async def test_repos_screen_structure_empty(home: Path) -> None:
    from textual.widgets import DataTable

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 0
        assert table.columns  # columns set up


@pytest.mark.asyncio
async def test_repos_screen_structure_one_repo(home: Path, tmp_path: Path) -> None:
    from textual.widgets import DataTable

    _setup_repo_with_skills(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_skills_screen_structure_two_skills(home: Path, tmp_path: Path) -> None:
    from textual.widgets import DataTable

    _setup_repo_with_skills(
        tmp_path,
        {
            "skills/review/SKILL.md": "# Review\n\nReview a PR.\n",
            "skills/format/SKILL.md": "# Format\n\nApply formatting.\n",
        },
    )
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_rules_screen_structure_with_rule(home: Path, tmp_path: Path) -> None:
    from textual.widgets import DataTable

    _register_rule_repo(tmp_path, ["be-concise"])
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


# ---------- Single bitmap backstop per area (layout regression) ----------


skip_in_ci = pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Bitmap snapshots are rendered with the local terminal; they differ across platforms.",
)


@skip_in_ci
def test_snapshot_main_layout(home: Path, snap_compare) -> None:  # type: ignore[no-untyped-def]
    """One bitmap test per area — catches gross layout regressions. Update
    via `pytest tests/tui --snapshot-update` after intentional UI changes."""
    assert snap_compare(AimApp())


@skip_in_ci
def test_snapshot_skills_populated(
    home: Path,
    tmp_path: Path,
    snap_compare,  # type: ignore[no-untyped-def]
) -> None:
    _setup_repo_with_skills(tmp_path, {"skills/review/SKILL.md": "# Review\n\nReview a PR.\n"})
    assert snap_compare(AimApp(), press=["s"])
