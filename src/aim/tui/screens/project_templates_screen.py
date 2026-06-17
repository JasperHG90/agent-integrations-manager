"""Project templates screen — save, list, apply, and delete reusable project bundles.

A project template captures a project's rules, skills, subagents, MCP servers,
symlinks, instruction template, and layout profile. It is stored as a `Profile`
JSON under `user_config_dir/profiles/`.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static

from aim.core import profiles as profiles_mod
from aim.tui.modals.confirm import ConfirmModal
from aim.tui.modals.export_toml import ExportTomlModal
from aim.tui.modals.project_picker import ProjectPick, ProjectPickerModal
from aim.tui.modals.template_edit import TemplateEditModal, TemplateEditResult
from aim.tui.modals.template_save import TemplateSaveModal, TemplateSaveResult
from aim.tui.screens.template_builder_screen import TemplateBuilderScreen


class ProjectTemplatesScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("n", "new_template", "New"),
        ("a", "save_template", "Save"),
        ("e", "edit_current", "Edit"),
        ("shift+e", "export_current", "Export TOML"),
        ("u", "update_from_project", "Update from project"),
        ("enter", "view_current", "View"),
        ("v", "view_current", "View"),
        ("p", "apply_current", "Apply"),
        ("d", "delete_current", "Delete"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()

    def compose(self) -> ComposeResult:
        yield Static("Project templates", id="title", markup=False)
        yield DataTable(id="templates-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[n] New  [a] Save  [e] Edit  [E] Export TOML  [u] Update from project  "
            "[enter/v] View  [p] Apply  [d] Delete  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        table = self.query_one("#templates-table", DataTable)
        table.add_columns("name", "instruction_template", "profile", "skills", "subagents", "mcp", "rules")
        self._populate()
        table.focus()

    def on_screen_resume(self) -> None:
        self._populate()

    def _selected_name(self) -> str | None:
        table = self.query_one("#templates-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _populate(self) -> None:
        table = self.query_one("#templates-table", DataTable)
        selected = self._selected_name()
        table.clear()
        profiles = profiles_mod.list_profiles()
        if not profiles:
            self._status("no templates — press [a] to save the current project")
            return
        for p in profiles:
            table.add_row(
                p.name,
                p.instruction_template,
                p.layout_profile or "—",
                str(len(p.skills)),
                str(len(p.agents)),
                str(len(p.mcp_servers)),
                str(len(p.rules)),
                key=p.name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{len(profiles)} template(s)")

    def action_new_template(self) -> None:
        self.app.push_screen(TemplateBuilderScreen(), self._on_new)

    def _on_new(self, result: None) -> None:
        self._populate()

    def action_save_template(self) -> None:
        self.app.push_screen(TemplateSaveModal(), self._on_save)

    def _on_save(self, result: TemplateSaveResult | None) -> None:
        if result is None:
            return
        try:
            profile = profiles_mod.from_project(result.name, self._project_root)
        except Exception as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        try:
            profiles_mod.save(profile)
        except profiles_mod.ProfileNameError as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        self.app.notify(f"saved template {result.name}")
        self._populate()

    def action_edit_current(self) -> None:
        name = self._selected_name()
        if name is None:
            self._notify_or_status("select a template to edit")
            return
        try:
            profile = profiles_mod.load(name)
        except profiles_mod.ProfileNotFoundError:
            self._status(f"template {name!r} not found")
            return
        self._pending_edit = profile.name
        self.app.push_screen(TemplateEditModal(profile), self._on_edit)

    def _on_edit(self, result: TemplateEditResult | None) -> None:
        old_name = getattr(self, "_pending_edit", None)
        self._pending_edit = None
        if result is None or old_name is None:
            return
        try:
            profile = profiles_mod.load(old_name)
        except profiles_mod.ProfileNotFoundError:
            self.app.notify(f"template {old_name!r} not found", severity="error")
            return
        new_profile = profile.model_copy(
            update={
                "name": result.name,
                "instruction_template": result.instruction_template,
                "layout_profile": result.layout_profile,
                "agent_dialect": result.agent_dialect,
                "rules": list(result.rules),
                "skills": [s for s in profile.skills if s.qualified_name in result.skills],
                "agents": [a for a in profile.agents if a.qualified_name in result.agents],
                "mcp_servers": [m for m in profile.mcp_servers if m.alias in result.mcp_servers],
            }
        )
        if result.name != profile.name:
            profiles_mod.delete(profile.name)
        try:
            profiles_mod.save(new_profile)
        except profiles_mod.ProfileNameError as exc:
            self.app.notify(f"edit failed: {exc}", severity="error")
            return
        self.app.notify(f"updated template {result.name!r}")
        self._populate()

    def action_update_from_project(self) -> None:
        name = self._selected_name()
        if name is None:
            self._notify_or_status("select a template to update")
            return
        self._pending_update = name
        self.app.push_screen(
            ProjectPickerModal(
                f"Update template {name!r} from project",
                action_label="Update",
                initial_project=self._project_root,
            ),
            self._on_update_from_project,
        )

    def _on_update_from_project(self, result: ProjectPick | None) -> None:
        name = getattr(self, "_pending_update", None)
        self._pending_update = None
        if result is None or name is None:
            return
        try:
            profile = profiles_mod.from_project(name, result.project_root)
            profiles_mod.save(profile)
        except profiles_mod.ProfileNameError as exc:
            self.app.notify(f"update failed: {exc}", severity="error")
            return
        except Exception as exc:
            self.app.notify(f"update failed: {exc}", severity="error")
            return
        self.app.notify(
            f"updated template {name!r} from {result.project_root}",
            title="Template updated",
        )
        self._populate()

    def action_view_current(self) -> None:
        name = self._selected_name()
        if name is None:
            self._notify_or_status("select a template to view")
            return
        try:
            profile = profiles_mod.load(name)
        except profiles_mod.ProfileNotFoundError:
            self._status(f"template {name!r} not found")
            return
        lines = [
            f"name: {profile.name}",
            f"instruction_template: {profile.instruction_template}",
            f"layout_profile: {profile.layout_profile or '—'}",
            f"agent_dialect: {profile.agent_dialect or '—'}",
            f"symlinks: {', '.join(profile.symlinks) if profile.symlinks else '—'}",
            f"rules: {', '.join(profile.rules) if profile.rules else '—'}",
        ]
        if profile.skills:
            lines.append("skills:")
            for s in profile.skills:
                pin = f" pin={s.pin}" if s.pin else ""
                track = f" track={s.track}" if s.track else ""
                lines.append(f"  - {s.qualified_name}{pin}{track}")
        if profile.agents:
            lines.append("subagents:")
            for a in profile.agents:
                pin = f" pin={a.pin}" if a.pin else ""
                track = f" track={a.track}" if a.track else ""
                lines.append(f"  - {a.qualified_name}{pin}{track}")
        if profile.mcp_servers:
            lines.append("mcp servers:")
            for m in profile.mcp_servers:
                lines.append(f"  - {m.registry_name} as {m.alias}")
        self.app.push_screen(
            ConfirmModal("\n".join(lines), confirm_label="Close"),
            lambda _: None,
        )

    def action_apply_current(self) -> None:
        name = self._selected_name()
        if name is None:
            self._notify_or_status("select a template to apply")
            return
        self._pending_apply = name
        self.app.push_screen(
            ProjectPickerModal(
                f"Apply template {name!r}",
                action_label="Apply",
                initial_project=self._project_root,
            ),
            self._on_apply_picked,
        )

    def _on_apply_picked(self, result: ProjectPick | None) -> None:
        name = getattr(self, "_pending_apply", None)
        self._pending_apply = None
        if result is None or name is None:
            return
        try:
            apply_result = profiles_mod.apply(name, result.project_root)
        except Exception as exc:
            self.app.notify(f"apply failed: {exc}", severity="error")
            return
        parts: list[str] = []
        if apply_result.installed_skills:
            parts.append(f"{len(apply_result.installed_skills)} skill(s)")
        if apply_result.installed_agents:
            parts.append(f"{len(apply_result.installed_agents)} agent(s)")
        if apply_result.installed_mcp:
            parts.append(f"{len(apply_result.installed_mcp)} MCP(s)")
        skipped = (
            apply_result.skipped_skills + apply_result.skipped_agents + apply_result.skipped_mcp
        )
        msg = f"applied {name} to {result.project_root}"
        if parts:
            msg += f" ({', '.join(parts)})"
        if skipped:
            msg += f" — skipped {len(skipped)} unavailable item(s)"
        self.app.notify(msg, title="Template applied")

    def action_delete_current(self) -> None:
        name = self._selected_name()
        if name is None:
            self._notify_or_status("select a template to delete")
            return

        def _on_confirm(yes: bool | None) -> None:
            if yes is not True:
                return
            if profiles_mod.delete(name):
                self.app.notify(f"deleted template {name!r}")
            else:
                self.app.notify(f"template {name!r} not found", severity="error")
            self._populate()

        self.app.push_screen(ConfirmModal(f"Delete project template {name!r}?"), _on_confirm)

    def action_export_current(self) -> None:
        name = self._selected_name()
        if name is None:
            self._notify_or_status("select a template to export")
            return
        try:
            profile = profiles_mod.load(name)
        except profiles_mod.ProfileNotFoundError:
            self._status(f"template {name!r} not found")
            return
        self.app.push_screen(
            ExportTomlModal(profile, initial_path=f"{profile.name}.toml"),
            lambda _: self._populate(),
        )

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _notify_or_status(self, msg: str) -> None:
        if self.query_one("#templates-table", DataTable).row_count == 0:
            self.app.notify("save a project as a template first", severity="warning")
        else:
            self._status(msg)
