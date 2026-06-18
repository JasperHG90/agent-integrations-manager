from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static

from aim.core import init, profiles, repos
from aim.tui.app import AimApp
from aim.tui.modals.template_edit import TemplateEditModal
from aim.tui.modals.template_save import TemplateSaveModal
from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen
from aim.tui.widgets import ToggleRow
from tests.fixtures import git_fixtures


def _register_rule_repo(tmp_path: Path, names: list[str]) -> None:
    files = {f"rules/{n}.md": f"{n} body\n" for n in names}
    files["README.md"] = "x\n"
    working = git_fixtures.make_source_repo(tmp_path / "rsrc", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "rbare.git")
    repos.add("rr", f"file://{bare}")


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
async def test_templates_screen_edits_template(home: Path, project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    profiles.save(profiles.from_project("old", project_root))
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TemplateEditModal)
        app.screen.query_one("#name", Input).value = "new"
        await pilot.click("#go")
        await pilot.pause()
        assert [p.name for p in profiles.list_profiles()] == ["new"]


@pytest.mark.asyncio
async def test_template_edit_checkbox_toggles_uncheck_all(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """Checkboxes in the edit modal must be clickable and uncheckable."""
    _register_rule_repo(tmp_path, ["rule-one"])
    init.run(init.InitOptions(project_root=project_root))
    profile = profiles.Profile(
        name="tpl",
        instruction_template="default",
        rules=["rr/rule-one"],
        skills=[profiles.ProfileSkill(qualified_name="repo/skill")],
        agents=[profiles.ProfileAgent(qualified_name="repo/agent")],
        mcp_servers=[profiles.ProfileMcpServer(registry_name="srv", alias="srv")],
    )
    profiles.save(profile)

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TemplateEditModal)

        rule_cb = app.screen.query_one("#rule-rr-rule-one", ToggleRow)
        skill_cb = app.screen.query_one("#skill-repo-skill", ToggleRow)
        agent_cb = app.screen.query_one("#agent-repo-agent", ToggleRow)
        mcp_cb = app.screen.query_one("#mcp-srv", ToggleRow)

        assert rule_cb.value is True
        assert skill_cb.value is True
        assert agent_cb.value is True
        assert mcp_cb.value is True

        # Uncheck every item by clicking it.
        await pilot.click("#rule-rr-rule-one")
        await pilot.click("#skill-repo-skill")
        await pilot.click("#agent-repo-agent")
        await pilot.click("#mcp-srv")
        await pilot.pause()

        assert rule_cb.value is False
        assert skill_cb.value is False
        assert agent_cb.value is False
        assert mcp_cb.value is False

        await pilot.click("#go")
        await pilot.pause()

    saved = profiles.load("tpl")
    assert saved.rules == []
    assert saved.skills == []
    assert saved.agents == []
    assert saved.mcp_servers == []


@pytest.mark.asyncio
async def test_template_edit_togglerow_space_toggles(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """Focused ToggleRow must toggle on Space."""
    _register_rule_repo(tmp_path, ["rule-one"])
    init.run(init.InitOptions(project_root=project_root))
    profile = profiles.Profile(
        name="tpl",
        instruction_template="default",
        rules=["rr/rule-one"],
    )
    profiles.save(profile)

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TemplateEditModal)

        rule_cb = app.screen.query_one("#rule-rr-rule-one", ToggleRow)
        assert rule_cb.value is True
        rule_cb.focus()
        await pilot.press("space")
        await pilot.pause()
        assert rule_cb.value is False
        await pilot.press("space")
        await pilot.pause()
        assert rule_cb.value is True


@pytest.mark.asyncio
async def test_template_edit_togglerow_reaches_by_tab(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """Tab navigation must reach ToggleRow and Space must toggle it."""
    _register_rule_repo(tmp_path, ["rule-tab"])
    init.run(init.InitOptions(project_root=project_root))
    profile = profiles.Profile(
        name="tpl",
        instruction_template="default",
        rules=["rr/rule-tab"],
    )
    profiles.save(profile)

    app = AimApp(project_root=project_root)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TemplateEditModal)

        rule_cb = app.screen.query_one("#rule-rr-rule-tab", ToggleRow)
        assert rule_cb.value is True
        # Tab from the focused name input down to the rule toggle.
        for _ in range(6):
            await pilot.press("tab")
            await pilot.pause()
            if app.focused is rule_cb:
                break
        assert app.focused is rule_cb
        await pilot.press("space")
        await pilot.pause()
        assert rule_cb.value is False
