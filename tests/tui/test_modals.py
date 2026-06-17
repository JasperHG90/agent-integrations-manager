"""Smoke tests that actually OPEN every modal from every screen.

Without these the original suite missed a `BadIdentifier: 'mirror-CLAUDE.md'` —
filenames contain dots and Textual ids can't. Each test here drives the key
that pops a modal and asserts the modal's class is on top of the screen stack.

ESC-cancel tests verify that hitting Escape while an input is focused dismisses
the install/initialize/add modals and returns to the previous screen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from aim.core import repos, rules
from aim.tui.app import AimApp
from aim.tui.modals.agent_install import AgentInstallModal
from aim.tui.modals.confirm import ConfirmModal
from aim.tui.modals.init_modal import InitModal
from aim.tui.modals.project_picker import ProjectPickerModal
from aim.tui.modals.repo_add import RepoAddModal
from aim.tui.modals.rule_add import RuleAddModal
from aim.tui.modals.skill_install import SkillInstallModal
from aim.tui.widgets import ToggleRow
from tests.fixtures import git_fixtures


def _bare_with_skills(tmp_path: Path) -> Path:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n",
            "skills/bar/SKILL.md": "# bar\n",
        },
    )
    return git_fixtures.make_bare_remote(working, tmp_path / "bare.git")


@pytest.mark.asyncio
async def test_main_screen_opens_init_modal(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InitModal)


@pytest.mark.asyncio
async def test_repos_screen_opens_add_modal(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, RepoAddModal)


@pytest.mark.asyncio
async def test_repos_screen_remove_opens_confirm(home: Path, tmp_path: Path) -> None:
    bare = _bare_with_skills(tmp_path)
    repos.add("anth", f"file://{bare}")
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)


@pytest.mark.asyncio
async def test_rules_screen_opens_add_modal(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, RuleAddModal)


@pytest.mark.asyncio
async def test_rules_screen_edit_opens_modal(home: Path) -> None:
    rules.add("existing", "body", is_default=True)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, RuleAddModal)


@pytest.mark.asyncio
async def test_rules_screen_delete_opens_confirm(home: Path) -> None:
    rules.add("doomed", "body")
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)


@pytest.mark.asyncio
async def test_skills_screen_install_opens_modal(home: Path, tmp_path: Path) -> None:
    bare = _bare_with_skills(tmp_path)
    repos.add("anth", f"file://{bare}")
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, SkillInstallModal)


@pytest.mark.asyncio
async def test_init_modal_submits_with_selected_symlinks(home: Path, project_root: Path) -> None:
    """End-to-end: open init modal, tick a symlink checkbox, submit, verify file."""
    rules.add("focus", "Focus.", is_default=True)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, InitModal)

        from textual.widgets import Input

        modal.query_one("#project-root", Input).value = str(project_root)
        modal.query_one(f"#{InitModal._symlink_id('CLAUDE.md')}", ToggleRow).value = True
        await pilot.pause()
        # Click the Initialize button.
        from textual.widgets import Button

        for btn in modal.query(Button):
            if btn.id == "go":
                btn.press()
                break
        await pilot.pause()
        await pilot.pause()

    # init now writes the aim.toml declarations file only.
    decl_path = project_root / "aim.toml"
    assert decl_path.exists()
    from aim.core import declarations

    decl = declarations.load(project_root)
    assert "CLAUDE.md" in decl.symlinks
    assert "GEMINI.md" not in decl.symlinks


@pytest.mark.asyncio
async def test_repo_add_modal_creates_repo(home: Path, tmp_path: Path) -> None:
    bare = _bare_with_skills(tmp_path)
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, RepoAddModal)
        from textual.widgets import Button, Input

        modal.query_one("#alias", Input).value = "demo"
        modal.query_one("#url", Input).value = f"file://{bare}"
        await pilot.pause()
        for btn in modal.query(Button):
            if btn.id == "add":
                btn.focus()
                await pilot.pause()
                btn.press()
                break
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert repos.get("demo").alias == "demo"


@pytest.mark.asyncio
async def test_rule_add_modal_creates_rule(home: Path) -> None:
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, RuleAddModal)
        from textual.widgets import Button, Input, TextArea

        modal.query_one("#name", Input).value = "from-tui"
        modal.query_one("#body", TextArea).text = "Body from the TUI."
        modal.query_one("#default", ToggleRow).value = True
        await pilot.pause()
        for btn in modal.query(Button):
            if btn.id == "save":
                btn.press()
                break
        await pilot.pause()

    saved = rules.get("from-tui")
    assert saved.body == "Body from the TUI."
    assert saved.is_default is True


@pytest.mark.asyncio
async def test_init_modal_submits_on_enter_from_input(home: Path, project_root: Path) -> None:
    """Pressing Enter inside a focused Input must submit the modal."""
    rules.add("enter-rule", "Rule.", is_default=True)
    app = AimApp()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, InitModal)
        modal.query_one("#project-root", Input).value = str(project_root)
        await pilot.press("enter")
        await pilot.pause()

    assert (project_root / "aim.toml").exists()


@pytest.mark.asyncio
async def test_repo_add_modal_submits_on_enter_from_input(home: Path, tmp_path: Path) -> None:
    """Pressing Enter inside a focused Input must submit the repo add modal."""
    bare = _bare_with_skills(tmp_path)
    app = AimApp()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, RepoAddModal)
        modal.query_one("#alias", Input).value = "enter-demo"
        modal.query_one("#url", Input).value = f"file://{bare}"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert repos.get("enter-demo").alias == "enter-demo"


@pytest.mark.asyncio
async def test_init_modal_submits_on_enter_from_checkbox(home: Path, project_root: Path) -> None:
    """Pressing Enter with a checkbox focused must still submit the modal."""
    rules.add("checkbox-enter-rule", "Rule.", is_default=True)
    app = AimApp()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, InitModal)
        modal.query_one("#project-root", Input).value = str(project_root)
        # Move focus to a symlink checkbox.
        cb = modal.query_one(f"#{InitModal._symlink_id('CLAUDE.md')}", ToggleRow)
        cb.focus()
        await pilot.press("enter")
        await pilot.pause()

    assert (project_root / "aim.toml").exists()


@pytest.mark.asyncio
async def test_install_modal_buttons_remain_visible(home: Path, project_root: Path) -> None:
    """Buttons at the bottom of a scrollable install modal must not fall off."""
    from textual.containers import Vertical
    from textual.widgets import Button

    app = AimApp()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, InitModal)
        modal_container = modal.query_one(".modal", Vertical)
        go_btn = modal.query_one("#go", Button)
        # The save/primary button must be fully inside the modal container.
        btn_region = go_btn.region
        center_x = btn_region.x + btn_region.width // 2
        center_y = btn_region.y + btn_region.height // 2
        assert modal_container.region.contains(center_x, center_y)


@pytest.mark.asyncio
async def test_init_modal_esc_dismisses(home: Path) -> None:
    """ESC must dismiss the Initialize modal even with the project input focused."""
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(InitModal())
        await pilot.pause()
        assert isinstance(app.screen, InitModal)
        assert isinstance(app.focused, Input)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InitModal)


@pytest.mark.asyncio
async def test_repo_add_modal_esc_dismisses(home: Path) -> None:
    """ESC must dismiss the Add repo modal even with the alias input focused."""
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RepoAddModal())
        await pilot.pause()
        assert isinstance(app.screen, RepoAddModal)
        assert isinstance(app.focused, Input)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RepoAddModal)


@pytest.mark.asyncio
async def test_skill_install_modal_esc_dismisses(home: Path) -> None:
    """ESC must dismiss the skill install modal even with the project input focused."""
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SkillInstallModal("anth/foo"))
        await pilot.pause()
        assert isinstance(app.screen, SkillInstallModal)
        assert isinstance(app.focused, Input)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SkillInstallModal)


@pytest.mark.asyncio
async def test_agent_install_modal_esc_dismisses(home: Path) -> None:
    """ESC must dismiss the agent install modal even with the project input focused."""
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AgentInstallModal("anth/bar"))
        await pilot.pause()
        assert isinstance(app.screen, AgentInstallModal)
        assert isinstance(app.focused, Input)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, AgentInstallModal)


@pytest.mark.asyncio
async def test_project_picker_modal_esc_dismisses(home: Path) -> None:
    """ESC must dismiss the project picker modal even with the project input focused."""
    app = AimApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ProjectPickerModal("Pick project"))
        await pilot.pause()
        assert isinstance(app.screen, ProjectPickerModal)
        assert isinstance(app.focused, Input)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ProjectPickerModal)
