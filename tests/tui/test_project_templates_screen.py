from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static

from aim.core import init, install, manifest, plugin_install, profiles, repos
from aim.tui.app import AimApp
from aim.tui.modals.project_picker import ProjectPick
from aim.tui.modals.template_save import TemplateSaveModal
from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen
from aim.tui.screens.template_builder_screen import TemplateBuilderScreen
from tests.fixtures import git_fixtures


@pytest.mark.asyncio
async def test_templates_screen_empty_when_no_templates(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        status = app.screen.query_one("#status", Static)
        assert "no templates" in str(status.render())


@pytest.mark.asyncio
async def test_templates_screen_saves_current_project(home: Path, project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, TemplateSaveModal)
        app.screen.query_one("#name", Input).value = "mytpl"
        await pilot.click("#go")
        await pilot.pause()
        assert [p.name for p in profiles.list_profiles()] == ["mytpl"]
        table = app.screen.query_one("#templates-table", DataTable)
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "mytpl"


@pytest.mark.asyncio
async def test_templates_screen_apply_runs_off_thread(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Applying from the TUI must not crash with "asyncio.run() cannot be called from
    # a running event loop": apply runs lock+sync, so it has to go off the UI thread.
    from aim.core import install

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    init.run(init.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")
    profiles.save(profiles.from_project("src", project_root))

    target = tmp_path / "target"
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = ProjectTemplatesScreen(project_root)
        app.push_screen(screen)
        await pilot.pause()
        screen._pending_apply = "src"
        screen._on_apply_picked(ProjectPick(project_root=target))
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert [s.qualified_name for s in manifest.load(target).skills] == ["anth/foo"]


def _claude_plugin_repo(tmp_path: Path) -> str:
    """Register a repo (alias `sp`) exposing one whole-repo claude plugin; return url."""
    import json

    working = git_fixtures.make_source_repo(
        tmp_path / "psrc",
        files={
            ".claude-plugin/marketplace.json": json.dumps(
                {"name": "m", "plugins": [{"name": "superpowers", "source": "./"}]}
            ),
            ".claude-plugin/plugin.json": json.dumps({"name": "superpowers"}),
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "pbare.git")
    url = f"file://{bare}"
    repos.add("sp", url)
    return url


@pytest.mark.asyncio
async def test_edit_opens_builder_seeded_with_template(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Editing must open the panel-based builder (not a flat list), seeded with the
    # template's contents — so every kind is add/removable, not just toggle-off.
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    init.run(init.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")
    profiles.save(profiles.from_project("old", project_root))

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TemplateBuilderScreen)
        assert app.screen.query_one("#name", Input).value == "old"
        assert app.screen.query_one("#skills-table", DataTable).row_count == 1
        # A plugins panel is present in the builder (it was absent from the old modal).
        app.screen.query_one("#plugins-table", DataTable)
        app.screen.query_one("#name", Input).value = "renamed"
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert "renamed" in [p.name for p in profiles.list_profiles()]


@pytest.mark.asyncio
async def test_view_shows_plugins_in_scrollable_modal(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Viewing a template must show plugins AND MCP inside a scrollable body, so a
    # long template doesn't hide the bottom rows behind an unscrollable wall of text.
    from textual.containers import VerticalScroll

    from aim.tui.modals.confirm import ConfirmModal

    _claude_plugin_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    plugin_install.install_plugin(project_root, "sp/superpowers")
    profiles.save(profiles.from_project("tpl", project_root))

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        body = app.screen.query_one(".modal-body", Static)
        text = str(body.render())
        assert "plugins:" in text
        assert "sp/superpowers" in text
        # The body lives inside a scroll container so long content is reachable.
        assert app.screen.query_one(VerticalScroll)


@pytest.mark.asyncio
async def test_builder_adds_plugin_to_template(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # The builder must let you add a plugin via the picker and persist it.
    from aim.tui.modals.plugin_picker import PluginPickerModal

    _claude_plugin_repo(tmp_path)
    app = AimApp(project_root=project_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        app.screen.query_one("#plugins-table", DataTable).focus()
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PluginPickerModal)
        app.screen.query_one("#plugins-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TemplateBuilderScreen)
        table = app.screen.query_one("#plugins-table", DataTable)
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "sp/superpowers"
        app.screen.query_one("#name", Input).value = "withplugin"
        await pilot.press("ctrl+s")
        await pilot.pause()

    saved = profiles.load("withplugin")
    assert [(p.qualified_name, p.flavor) for p in saved.plugins] == [("sp/superpowers", "claude")]
