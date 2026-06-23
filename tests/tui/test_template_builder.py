"""Tests for the template builder screen and its pickers."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import DataTable, Input

from aim.core import profiles, repos
from aim.tui.app import AimApp
from aim.tui.modals.agent_picker import AgentPickerModal
from aim.tui.modals.export_toml import ExportTomlModal
from aim.tui.modals.import_toml import ImportTomlModal
from aim.tui.modals.mcp_picker import McpPickerModal
from aim.tui.modals.rule_picker import RulePickerModal
from aim.tui.modals.skill_picker import SkillPickerModal
from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen
from aim.tui.screens.template_builder_screen import TemplateBuilderScreen
from tests.fixtures import git_fixtures


def _register_rule_repo(tmp_path: Path, names: list[str]) -> None:
    files = {f"rules/{n}.md": f"{n} body\n" for n in names}
    files["README.md"] = "x\n"
    working = git_fixtures.make_source_repo(tmp_path / "rsrc", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "rbare.git")
    repos.add("rr", f"file://{bare}")


def _register_skill_rule_repo(tmp_path: Path) -> str:
    """Register a repo (alias `rr`) with one skill and one rule. Return its url."""
    working = git_fixtures.make_source_repo(
        tmp_path / "srsrc",
        files={
            "skills/skill/SKILL.md": "# skill\n",
            "rules/export-rule.md": "rule body\n",
            "README.md": "x\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "srbare.git")
    url = f"file://{bare}"
    repos.add("rr", url)
    return url


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


@pytest.mark.asyncio
async def test_builder_adds_rule(home: Path, project_root: Path, tmp_path: Path) -> None:
    _register_rule_repo(tmp_path, ["builder-rule"])
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
        assert table.get_row_at(0)[0] == "rr/builder-rule"


@pytest.mark.asyncio
async def test_builder_skips_duplicate_rule(home: Path, project_root: Path, tmp_path: Path) -> None:
    _register_rule_repo(tmp_path, ["dup-rule"])
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
async def test_builder_removes_rule(home: Path, project_root: Path, tmp_path: Path) -> None:
    _register_rule_repo(tmp_path, ["remove-rule"])
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
        'description = "imported desc"\n'
        'layout_profile = "claude"\n'
        'symlinks = ["CLAUDE.md", "GEMINI.md"]\n'
        "[[rule]]\n"
        'qualified_name = "repo/imported-rule"\n'
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
        # Project-layout metadata is restored into its inputs, not dropped.
        assert app.screen.query_one("#description", Input).value == "imported desc"
        assert app.screen.query_one("#layout-profile", Input).value == "claude"
        assert app.screen.query_one("#symlinks", Input).value == "CLAUDE.md, GEMINI.md"
        rules_table = app.screen.query_one("#rules-table", DataTable)
        assert rules_table.row_count == 1
        assert rules_table.get_row_at(0)[0] == "repo/imported-rule"
        skills_table = app.screen.query_one("#skills-table", DataTable)
        assert skills_table.row_count == 1
        assert skills_table.get_row_at(0)[0] == "repo/skill"


@pytest.mark.asyncio
async def test_builder_exports_toml(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _register_skill_rule_repo(tmp_path)
    profile = profiles.Profile(
        name="export-me",
        description="a service template",
        layout_profile="claude",
        symlinks=["CLAUDE.md"],
        rules=[profiles.ProfileRule(qualified_name="rr/export-rule")],
        skills=[profiles.ProfileSkill(qualified_name="rr/skill")],
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
    # The source repo is recorded so the template can reconstruct it elsewhere.
    assert "[[repo]]" in text
    assert 'alias = "rr"' in text
    assert f'url = "{url}"' in text
    assert "[[rule]]" in text
    assert 'qualified_name = "rr/export-rule"' in text
    assert "[[skill]]" in text
    assert 'qualified_name = "rr/skill"' in text
    # Each artifact is frozen to a SHA.
    assert text.count("sha = ") == 2
    # Project-layout metadata is preserved, not silently dropped.
    assert 'description = "a service template"' in text
    assert 'layout_profile = "claude"' in text
    assert "CLAUDE.md" in text


@pytest.mark.asyncio
async def test_builder_export_unresolved_artifact_shows_error(
    home: Path, project_root: Path
) -> None:
    profile = profiles.Profile(
        name="broken",
        skills=[profiles.ProfileSkill(qualified_name="ghost/skill")],
    )
    app = AimApp(project_root=project_root)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(TemplateBuilderScreen(profile))
        await pilot.pause()
        _unfocus_input(app)
        await pilot.press("e")
        await pilot.pause()
        # Enrichment fails (repo not registered) → export modal is not opened.
        assert isinstance(app.screen, TemplateBuilderScreen)
        assert not isinstance(app.screen, ExportTomlModal)


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
