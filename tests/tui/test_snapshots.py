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


def _setup_repo_with_plugin(tmp_path: Path) -> None:
    import json

    marketplace = {
        "name": "demo-market",
        "plugins": [{"name": "design-audit", "source": "./design-audit", "version": "1.0.0"}],
    }
    files = {
        ".claude-plugin/marketplace.json": json.dumps(marketplace),
        "design-audit/.claude-plugin/plugin.json": json.dumps({"name": "design-audit"}),
        "design-audit/skills/audit/SKILL.md": "# audit\n",
    }
    working = git_fixtures.make_source_repo(tmp_path / "psrc", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "pbare.git")
    repos.add("pm", f"file://{bare}")


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
async def test_plugins_screen_structure_empty(home: Path) -> None:
    from textual.widgets import DataTable

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 0
        assert table.columns  # columns set up


@pytest.mark.asyncio
async def test_plugins_screen_structure_one_plugin(home: Path, tmp_path: Path) -> None:
    from textual.widgets import DataTable

    _setup_repo_with_plugin(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


_OPENCODE_TARGET_TOML = """
name = "opencode"
[discover]
manifest = [".opencode/plugins/*.ts", ".opencode/plugins/*.js"]
name_from = "stem"
[register]
vendor_into = ".opencode/plugins/{name}.{ext}"
vendor_as = "file"
"""


@pytest.mark.asyncio
async def test_plugins_screen_shows_project_scoped_target(home: Path, tmp_path: Path) -> None:
    """A target spec in the PROJECT .aim/targets/ must surface in the TUI plugins screen,
    not just the CLI — the screen threads its project_root through to discovery."""
    from textual.widgets import DataTable

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={".opencode/plugins/logger.ts": "export const plugin = 1\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    # opencode is project-scoped here, so the machine-global index sees nothing.
    repos.add("a", f"file://{bare}", allow_empty=True)
    proj = tmp_path / "proj"
    (proj / ".aim" / "targets").mkdir(parents=True)
    (proj / ".aim" / "targets" / "opencode.toml").write_text(_OPENCODE_TARGET_TOML)

    app = AimApp(project_root=proj)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1  # the project-scoped target's plugin shows in the TUI


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
def test_snapshot_main_layout(home: Path, snap_compare, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One bitmap test per area — catches gross layout regressions. Update
    via `pytest tests/tui --snapshot-update` after intentional UI changes."""
    # Pin the rendered version so this bitmap does not drift on every release.
    import aim.tui.screens.main_screen as main_screen

    monkeypatch.setattr(main_screen, "__version__", "0.0.0-snapshot")
    assert snap_compare(AimApp())


@skip_in_ci
def test_snapshot_skills_populated(
    home: Path,
    tmp_path: Path,
    snap_compare,  # type: ignore[no-untyped-def]
) -> None:
    _setup_repo_with_skills(tmp_path, {"skills/review/SKILL.md": "# Review\n\nReview a PR.\n"})
    assert snap_compare(AimApp(), press=["s"])


# No bitmap backstop for the plugins screen: it renders each plugin's short SHA,
# which comes from a test-fixture git commit whose date (and thus SHA) is not
# pinned — the structural row-count tests above cover the screen deterministically.
