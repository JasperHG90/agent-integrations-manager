"""Regression tests for TUI defects found in the adversarial sweep."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable

from aim.core import repos
from aim.tui.app import AimApp
from aim.tui.modals.agent_view import AgentViewModal
from aim.tui.modals.plugin_view import PluginViewModal
from aim.tui.modals.rule_view import RuleViewModal
from aim.tui.modals.skill_view import SkillViewModal
from aim.tui.screens.layout_screen import LayoutScreen
from tests.fixtures import git_fixtures


@pytest.mark.asyncio
async def test_palette_open_layout_carries_project_root(home: Path, project_root: Path) -> None:
    """Regression: the palette entry constructed LayoutScreen() without the
    app's project_root, so profile writes landed in the shell's cwd."""
    from aim.tui.modals.palette import build_entries

    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        entry = next(e for e in build_entries(app) if e.label == "Open Layout")
        entry.handler()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LayoutScreen)
        assert screen._project_root == project_root.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modal_cls", [SkillViewModal, RuleViewModal, AgentViewModal, PluginViewModal]
)
async def test_view_modals_close_on_escape(home: Path, modal_cls) -> None:  # type: ignore[no-untyped-def]
    """Regression: bindings named 'action_close' resolved to a nonexistent
    action_action_close, leaving escape dead on every read-only view modal."""
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal_cls("x/y", "body text"))
        await pilot.pause()
        assert isinstance(app.screen, modal_cls)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, modal_cls)


_PROJECT_TARGET = """
name = "gemini"
[manifest]
file = "gemini-extension.json"
[register]
vendor_into = ".gemini/extensions/{name}"
"""


@pytest.mark.asyncio
async def test_targets_tab_lists_active_project_target(home: Path, project_root: Path) -> None:
    """A target file in .aim/targets/ is IN FORCE for the project — the Targets
    tab must show it (regression: only repo-indexed installables were listed,
    so the tab claimed 'no targets' while gemini.toml governed the project)."""
    from textual.widgets import TabbedContent

    from aim.tui.modals.skill_view import SkillViewModal

    targets_dir = project_root / ".aim" / "targets"
    targets_dir.mkdir(parents=True)
    (targets_dir / "gemini.toml").write_text(_PROJECT_TARGET)

    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LayoutScreen)
        table = screen.query_one("#targets-table", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "gemini"
        assert row[1] == "(this project)"
        assert "active" in row[2]

        screen.query_one(TabbedContent).active = "targets"
        await pilot.pause()
        # View opens the local file's TOML.
        screen.action_view_current()
        await pilot.pause()
        assert isinstance(app.screen, SkillViewModal)
        await pilot.press("escape")
        await pilot.pause()
        # Install on an active row is a no-op notify, not an install modal.
        screen.action_install_current()
        await pilot.pause()
        assert app.screen is screen


@pytest.mark.asyncio
async def test_plugins_screen_renders_before_overlay_discovery(
    home: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening Plugins must not block on project-target discovery (regression:
    a project-local target triggered git discovery across every registered repo
    on the paint path — 'EXTREMELY slow to open')."""
    import threading

    from aim.core import plugins
    from aim.tui.screens.plugin_screen import PluginsScreen

    release = threading.Event()
    calls: list[threading.Thread] = []

    def _blocking_overlay(root: Path, *, should_abort=None):  # type: ignore[no-untyped-def]
        calls.append(threading.current_thread())
        release.wait(timeout=10)
        return []

    monkeypatch.setattr(plugins, "project_overlay_rows", _blocking_overlay)
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = PluginsScreen(project_root=project_root)
        app.push_screen(screen)
        await pilot.pause()
        # The screen painted while discovery is still blocked, off-thread.
        assert "discovering" in str(screen.query_one("#status").render())
        assert all(t is not threading.main_thread() for t in calls)
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "discovering" not in str(screen.query_one("#status").render())


@pytest.mark.asyncio
async def test_registry_network_work_runs_on_daemon_threads(
    home: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry seeding/search must run on DAEMON threads, never Textual workers.

    The stall lives at interpreter exit: worker threads run on a non-daemon
    ThreadPoolExecutor that concurrent.futures JOINS at exit — after Textual
    teardown, so a wall-clock assertion around run_test() can never see it
    (that was the first, vacuous, version of this test). Assert the mechanism:
    the thread is a daemon and no Textual worker was created for it.
    """
    import threading

    from aim.core import mcp_registry
    from aim.tui.screens.mcp_screen import McpScreen

    release = threading.Event()
    seed_threads: list[threading.Thread] = []

    def _blocking_seed(names):  # type: ignore[no-untyped-def]
        seed_threads.append(threading.current_thread())
        release.wait(timeout=30)
        return {}

    monkeypatch.setattr(mcp_registry, "seed_default_servers", _blocking_seed)
    app = AimApp(project_root=project_root)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(McpScreen(project_root=project_root))
            await pilot.pause()
            # Both seeding sites (app mount + MCP screen defaults) fired…
            assert len(seed_threads) == 2
            # …on daemon threads that Textual does not track as workers.
            assert all(t.daemon for t in seed_threads)
            worker_names = {w.name for w in app.workers}
            assert not any("seed" in n or "defaults" in n for n in worker_names)
    finally:
        release.set()  # let the daemon threads finish


@pytest.mark.asyncio
async def test_targets_tab_surfaces_invalid_spec(home: Path, project_root: Path) -> None:
    """A broken .aim/targets TOML must be reported, not silently skipped —
    a failing spec is exactly what a user opens the tab to diagnose."""
    targets_dir = project_root / ".aim" / "targets"
    targets_dir.mkdir(parents=True)
    (targets_dir / "gemini.toml").write_text(_PROJECT_TARGET)
    (targets_dir / "bad.toml").write_text("name = 'broken'\nnot valid [ toml")

    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LayoutScreen)
        assert any("bad.toml" in w for w in screen._reported_spec_warnings)
        # The shared warning channel was drained (regression: it accumulated a
        # duplicate per keystroke and later dumped into plugin discovery).
        from aim.core import plugin_kinds

        for ch in "abc":
            screen._populate_targets(ch)
        assert plugin_kinds.take_load_warnings() == []
        assert sum("bad.toml" in w for w in screen._reported_spec_warnings) == 1


@pytest.mark.asyncio
async def test_repo_add_survives_concurrent_refresh(home: Path, tmp_path: Path) -> None:
    """Regression: all repos-screen flows shared one worker group, so pressing
    refresh mid-add cancelled the add and silently dropped the registration."""
    from aim.tui.modals.repo_add import RepoAddResult
    from aim.tui.screens.repos_screen import ReposScreen

    working_a = git_fixtures.make_source_repo(tmp_path / "a", files={"rules/a.md": "a\n"})
    bare_a = git_fixtures.make_bare_remote(working_a, tmp_path / "a.git")
    repos.add("a", f"file://{bare_a}")
    working_b = git_fixtures.make_source_repo(tmp_path / "b", files={"rules/b.md": "b\n"})
    bare_b = git_fixtures.make_bare_remote(working_b, tmp_path / "b.git")

    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ReposScreen()
        app.push_screen(screen)
        await pilot.pause()
        # Start the add, then immediately trigger a refresh of repo "a" while
        # the add worker is (potentially) still running.
        screen._on_add(
            RepoAddResult(alias="b", url=f"file://{bare_b}", default_ref="HEAD", allow_empty=False)
        )
        screen.action_refresh_current()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert repos.get("b").url == f"file://{bare_b}"  # the add completed
    assert repos.get("a") is not None
