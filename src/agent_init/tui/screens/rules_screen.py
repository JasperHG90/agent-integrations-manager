"""Rules library: list, add, edit, install into project, toggle default, delete."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static

from agent_init.core import rules
from agent_init.tui.modals.confirm import ConfirmModal
from agent_init.tui.modals.project_picker import ProjectPick, ProjectPickerModal
from agent_init.tui.modals.rule_add import RuleAddModal, RuleAddResult


class RulesScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("a", "add_rule", "Add"),
        ("e", "edit_rule", "Edit"),
        ("i", "install_rule", "Install"),
        ("d", "toggle_default", "Toggle default"),
        ("x", "delete_rule", "Delete"),
        ("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Rules Library", id="title", markup=False)
        yield DataTable(id="rules-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[a] Add  [e] Edit  [i] Install into project  [d] Toggle default  [x] Delete  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("name", "default", "description")
        self._populate()
        table.focus()

    def on_screen_resume(self) -> None:
        self._populate()

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        entries = rules.list_all()
        if not entries:
            self._status("no rules registered — press [a] to add one")
            return
        for r in entries:
            table.add_row(
                r.name,
                "✓" if r.is_default else "",
                r.description or "",
                key=r.name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{len(entries)} rule(s)")

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_add_rule(self) -> None:
        self.app.push_screen(RuleAddModal(is_edit=False), self._save_new_rule)

    def action_edit_rule(self) -> None:
        name = self._selected()
        if name is None:
            self._status("no row selected")
            return
        try:
            existing = rules.get(name)
        except rules.RuleNotFoundError as exc:
            self.app.notify(f"load failed: {exc}", severity="error")
            return
        self.app.push_screen(
            RuleAddModal(
                existing_body=existing.body,
                existing_name=existing.name,
                existing_description=existing.description or "",
                existing_default=existing.is_default,
                is_edit=True,
            ),
            self._save_existing_rule,
        )

    def _save_new_rule(self, result: RuleAddResult | None) -> None:
        if result is None:
            return
        # Confirm before overwriting an existing rule when in Add mode.
        try:
            rules.get(result.name)
            exists = True
        except rules.RuleNotFoundError:
            exists = False
        if exists:
            def _on_confirm(yes: bool | None) -> None:
                if yes is not True:
                    return
                self._write_rule(result)

            self.app.push_screen(
                ConfirmModal(
                    f"Rule {result.name!r} already exists. Overwrite?",
                    confirm_label="Overwrite",
                ),
                _on_confirm,
            )
            return
        self._write_rule(result)

    def _save_existing_rule(self, result: RuleAddResult | None) -> None:
        if result is None:
            return
        self._write_rule(result)

    def _write_rule(self, result: RuleAddResult) -> None:
        try:
            rules.add(
                result.name,
                result.body,
                description=result.description,
                is_default=result.is_default,
            )
        except rules.RuleNameError as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        self.app.notify(f"saved {result.name}")
        self._populate()

    def action_install_rule(self) -> None:
        name = self._selected()
        if name is None:
            self._status("no row selected")
            return
        self.app.push_screen(
            ProjectPickerModal(
                title=f"Install rule {name}",
                action_label="Install",
                helper="Project root (will be created if missing):",
            ),
            lambda pick: self._do_install(name, pick),
        )

    def _do_install(self, name: str, pick: ProjectPick | None) -> None:
        if pick is None:
            return
        try:
            rules.install_to_project(pick.project_root, name)
        except rules.RuleNotFoundError as exc:
            self.app.notify(f"install failed: {exc}", severity="error")
            return
        self.app.notify(
            f"installed rule {name} into {pick.project_root}",
            title="Rule installed",
        )

    def action_toggle_default(self) -> None:
        name = self._selected()
        if name is None:
            self._status("no row selected")
            return
        try:
            current = rules.get(name)
            rules.set_default(name, is_default=not current.is_default)
        except rules.RuleNotFoundError as exc:
            self.app.notify(f"toggle failed: {exc}", severity="error")
            return
        self._populate()

    def action_delete_rule(self) -> None:
        name = self._selected()
        if name is None:
            self._status("no row selected")
            return

        def _on_confirm(yes: bool | None) -> None:
            if yes is not True:
                return
            try:
                rules.delete(name)
            except rules.RuleNotFoundError as exc:
                self.app.notify(f"delete failed: {exc}", severity="error")
                return
            self.app.notify(f"deleted {name}")
            self._populate()

        self.app.push_screen(ConfirmModal(f"Delete rule {name!r}?"), _on_confirm)

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
