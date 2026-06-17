"""Layout profiles screen — list, add, edit, delete, and activate profiles."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static

from agent_init.core import layout_profiles, manifest
from agent_init.tui.modals.confirm import ConfirmModal
from agent_init.tui.modals.layout_profile_modal import (
    LayoutProfileModal,
    LayoutProfileResult,
)


class LayoutProfilesScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("a", "add_profile", "Add"),
        ("e", "edit_profile", "Edit"),
        ("x", "delete_profile", "Delete"),
        ("s", "set_active", "Set active"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()

    def compose(self) -> ComposeResult:
        yield Static("Profiles", id="title", markup=False)
        yield Static(
            "project = repo-only · global = DB cache + read-only repo copy",
            id="scope-help",
            markup=False,
        )
        yield DataTable(id="profiles-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[a] Add  [e] Edit  [x] Delete  [s] Set active  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.add_columns(
            "active", "name", "scope", "skills_dir", "rules_dir", "agents_md", "mirrors"
        )
        self._refresh()
        table.focus()

    def on_screen_resume(self) -> None:
        self._refresh()

    def _active_name(self) -> str | None:
        try:
            m = manifest.load(self._project_root)
            return m.layout_profile
        except manifest.ManifestNotFoundError:
            return None

    def _selected_name(self) -> str | None:
        table = self.query_one("#profiles-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _refresh(self) -> None:
        report = layout_profiles.sync_profiles(self._project_root)
        for warning in report.warnings:
            self.app.notify(warning, severity="warning")

        active = self._active_name()
        profiles = layout_profiles.list_profiles(self._project_root)
        table = self.query_one("#profiles-table", DataTable)
        selected = self._selected_name()
        table.clear()
        for p in profiles:
            is_active = ">" if p.name == active else ""
            scope = _scope_label(p)
            table.add_row(
                is_active,
                p.display_name or p.name,
                scope,
                p.skills_dir,
                p.rules_dir,
                p.agents_md,
                ",".join(p.mirrors) if p.mirrors else "-",
                key=p.name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{len(profiles)} profile(s)")

    def action_add_profile(self) -> None:
        self.app.push_screen(
            LayoutProfileModal(self._project_root),
            self._on_save,
        )

    def action_edit_profile(self) -> None:
        name = self._selected_name()
        if name is None:
            self._status("select a profile to edit")
            return
        try:
            profile = layout_profiles.get_profile(self._project_root, name)
        except layout_profiles.LayoutProfileNotFoundError:
            self._status(f"profile {name!r} not found")
            return
        self.app.push_screen(
            LayoutProfileModal(self._project_root, profile=profile),
            self._on_save,
        )

    def _on_save(self, result: LayoutProfileResult | None) -> None:
        if result is None:
            return
        try:
            if result.profile.scope == layout_profiles.LayoutProfileScope.GLOBAL:
                layout_profiles.save_global_profile(self._project_root, result.profile)
            else:
                layout_profiles.save_project_profile(self._project_root, result.profile)
        except Exception as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        # Rename: remove the old profile if the name changed.
        if result.original_name is not None and result.original_name != result.profile.name:
            layout_profiles.delete_global_profile(self._project_root, result.original_name)
        self.app.notify(f"saved profile {result.profile.name}")
        self._refresh()

    def action_delete_profile(self) -> None:
        name = self._selected_name()
        if name is None:
            self._status("select a profile to delete")
            return
        if name in (
            layout_profiles.BUILTIN_CLAUDE.name,
            layout_profiles.BUILTIN_GEMINI.name,
        ):
            self.app.notify("built-in profiles cannot be deleted", severity="error")
            return

        def _on_confirm(yes: bool | None) -> None:
            if yes is not True:
                return
            deleted = layout_profiles.delete_global_profile(self._project_root, name)
            if not deleted:
                self.app.notify(f"profile {name!r} not found", severity="error")
                return
            # If this was the active project profile, clear it from the manifest.
            try:
                m = manifest.load(self._project_root)
            except manifest.ManifestNotFoundError:
                m = None
            if m is not None and m.layout_profile == name:
                m.layout_profile = None
                manifest.save(self._project_root, m)
            self.app.notify(f"deleted profile {name!r}")
            self._refresh()

        self.app.push_screen(ConfirmModal(f"Delete layout profile {name!r}?"), _on_confirm)

    def action_set_active(self) -> None:
        name = self._selected_name()
        if name is None:
            self._status("select a profile to activate")
            return
        try:
            layout_profiles.set_active(self._project_root, name)
        except Exception as exc:
            self.app.notify(f"activation failed: {exc}", severity="error")
            return
        self.app.notify(f"active layout profile: {name}")
        self._refresh()

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)


def _scope_label(profile: layout_profiles.LayoutProfile) -> str:
    if profile.name in (layout_profiles.BUILTIN_CLAUDE.name, layout_profiles.BUILTIN_GEMINI.name):
        return "built-in"
    return profile.scope.value
