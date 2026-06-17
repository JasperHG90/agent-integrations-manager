"""Tests for the template builder screen and its pickers."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import DataTable, Input

from aim.core import profiles, rules
from aim.tui.app import AimApp
from aim.tui.modals.agent_picker import AgentPickerModal
from aim.tui.modals.export_toml import ExportTomlModal
from aim.tui.modals.import_toml import ImportTomlModal
from aim.tui.modals.mcp_picker import McpPickerModal
from aim.tui.modals.rule_picker import RulePickerModal
from aim.tui.modals.skill_picker import SkillPickerModal
from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen
from aim.tui.screens.template_builder_screen import TemplateBuilderScreen


def _unfocus_input(app: App[None]) -> None:
    """Move focus from the name/template inputs so screen keybindings fire."""
    app.screen.query_one("#skills-table", DataTable).focus()


@pytest.mark.asyncio
async def test_builder_opens_from_templates_screen(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectTemplatesScreen(project_root))
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, TemplateBuilderScreen)


@pytest.mark.asyncio
async def test_builder_saves_template(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        app.screen.query_one("#name", Input).value = "my-template"
        await pilot.press("ctrl+s")
        await pilot.pause()

    loaded = profiles.load("my-template")
    assert loaded.name == "my-template"
    assert loaded.instruction_template == "default"


@pytest.mark.asyncio
async def test_builder_adds_rule(home: Path, project_root: Path) -> None:
    rules.add("builder-rule", "A rule for the builder.")
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RulePickerModal)
        app.screen.query_one("#rules-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TemplateBuilderScreen)
        table = app.screen.query_one("#rules-table", DataTable)
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "builder-rule"


@pytest.mark.asyncio
async def test_builder_skips_duplicate_rule(home: Path, project_root: Path) -> None:
    rules.add("dup-rule", "Duplicate rule.")
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        for _ in range(2):
            _unfocus_input(app)
            await pilot.press("r")
            await pilot.pause()
            app.screen.query_one("#rules-table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
        table = app.screen.query_one("#rules-table", DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_builder_removes_rule(home: Path, project_root: Path) -> None:
    rules.add("remove-rule", "Rule to remove.")
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("r")
        await pilot.pause()
        app.screen.query_one("#rules-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#rules-table", DataTable).focus()
        await pilot.press("x")
        await pilot.pause()
        table = app.screen.query_one("#rules-table", DataTable)
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_builder_imports_toml(home: Path, project_root: Path, tmp_path: Path) -> None:
    toml_path = tmp_path / "imported.toml"
    toml_path.write_text(
        'name = "imported-template"\n'
        'instruction_template = "default"\n'
        'rules = ["imported-rule"]\n'
        "[[skill]]\n"
        'qualified_name = "repo/skill"\n',
        encoding="utf-8",
    )

    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen, ImportTomlModal)
        app.screen.query_one("#path", Input).value = str(toml_path)
        await pilot.click("#go")
        await pilot.pause()
        assert isinstance(app.screen, TemplateBuilderScreen)
        assert app.screen.query_one("#name", Input).value == "imported-template"
        rules_table = app.screen.query_one("#rules-table", DataTable)
        assert rules_table.row_count == 1
        assert rules_table.get_row_at(0)[0] == "imported-rule"
        skills_table = app.screen.query_one("#skills-table", DataTable)
        assert skills_table.row_count == 1
        assert skills_table.get_row_at(0)[0] == "repo/skill"


@pytest.mark.asyncio
async def test_builder_exports_toml(home: Path, project_root: Path, tmp_path: Path) -> None:
    profile = profiles.Profile(
        name="export-me",
        instruction_template="default",
        rules=["export-rule"],
        skills=[profiles.ProfileSkill(qualified_name="repo/skill")],
    )
    export_path = tmp_path / "exported.toml"

    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen(profile))
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportTomlModal)
        app.screen.query_one("#path", Input).value = str(export_path)
        await pilot.click("#go")
        await pilot.pause()

    assert export_path.exists()
    text = export_path.read_text(encoding="utf-8")
    assert 'name = "export-me"' in text
    assert 'instruction_template = "default"' in text
    assert 'rules = ["export-rule"]' in text
    assert "[[skill]]" in text
    assert 'qualified_name = "repo/skill"' in text


@pytest.mark.asyncio
async def test_builder_skill_picker_opens(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, SkillPickerModal)


@pytest.mark.asyncio
async def test_builder_agent_picker_opens(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, AgentPickerModal)


@pytest.mark.asyncio
async def test_builder_mcp_picker_opens(home: Path, project_root: Path) -> None:
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen())
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, McpPickerModal)
