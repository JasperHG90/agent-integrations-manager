"""Startup modal: pick a layout profile when none is active.

Returns `(profile_name, remember_as_global_default)` or `None` if cancelled.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Static

from agent_init.core import layout_profiles


class LayoutProfilePickerModal(ModalScreen[tuple[str, bool] | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "confirm", "Confirm"),
    ]

    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self._project_root = project_root
        self._profiles: list[layout_profiles.LayoutProfile] = []
        self._selected_name: str | None = None

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                "Choose a layout profile",
                classes="modal-title",
                markup=False,
            ),
            Static(
                "This determines where agent-init installs skills, rules, and mirror files.",
                markup=False,
            ),
            DataTable(id="profiles-table", cursor_type="row"),
            Checkbox("Remember as global default", id="remember"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Select", id="select", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.add_columns("name", "scope", "skills", "rules", "agents", "mirrors")
        self._populate()
        table.focus()

    def on_data_table_row_selected(self) -> None:
        self._confirm()

    def on_data_table_cursor_moved(self, event) -> None:  # type: ignore[no-untyped-def]
        table = self.query_one("#profiles-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key(event.coordinate)
        self._selected_name = str(row_key.value) if row_key and row_key.value is not None else None

    def _populate(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.clear()
        self._profiles = layout_profiles.list_profiles(self._project_root)
        for p in self._profiles:
            scope_label = _scope_label(p)
            table.add_row(
                p.display_name or p.name,
                scope_label,
                p.skills_dir,
                p.rules_dir,
                p.agents_md,
                ",".join(p.mirrors) if p.mirrors else "-",
                key=p.name,
            )
        if self._profiles:
            self._selected_name = self._profiles[0].name

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "select":
            self._confirm()
        else:
            self.dismiss(None)

    def action_confirm(self) -> None:
        self._confirm()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _confirm(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        if table.row_count == 0:
            self.query_one("#error", Static).update("no profiles available")
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        name = str(row_key.value) if row_key and row_key.value is not None else None
        if name is None:
            self.query_one("#error", Static).update("select a profile first")
            return
        remember = self.query_one("#remember", Checkbox).value
        self.dismiss((name, remember))


def _scope_label(profile: layout_profiles.LayoutProfile) -> str:
    if profile.name in (layout_profiles.BUILTIN_CLAUDE.name, layout_profiles.BUILTIN_GEMINI.name):
        return "built-in"
    return profile.scope.value
