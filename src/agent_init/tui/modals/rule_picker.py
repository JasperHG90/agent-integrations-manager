"""Modal: pick a registered rule by name or substring search."""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from agent_init.core import rules


@dataclass(frozen=True)
class RulePick:
    name: str


class _PickerDataTable(DataTable):
    """DataTable that forwards Enter to the picker's pick action."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.screen.action_pick()
        else:
            super().on_key(event)


class RulePickerModal(ModalScreen[RulePick | None]):
    BINDINGS = [
        Binding("escape", "action_cancel", "Cancel", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("enter", "action_pick", "Pick", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[rules.Rule] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Add rule", classes="modal-title", markup=False),
            Input(placeholder="search…", id="search-bar"),
            _PickerDataTable(id="rules-table", cursor_type="row"),
            Static("", id="status", markup=False),
            Horizontal(
                Button("Add", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        table = self.query_one("#rules-table", DataTable)
        table.add_columns("name", "description")
        self._populate("")
        self.query_one("#search-bar", Input).focus()

    def _populate(self, query: str) -> None:
        table = self.query_one("#rules-table", DataTable)
        table.clear()
        q = query.strip().lower()
        all_entries = rules.list_all()
        if q:
            self._entries = [
                r
                for r in all_entries
                if q in r.name.lower() or (r.description and q in r.description.lower())
            ]
        else:
            self._entries = all_entries
        if not self._entries:
            self._status("no rules registered — add one from the Rules screen")
            return
        for r in self._entries:
            table.add_row(
                r.name,
                r.description or "",
                key=r.name,
            )
        self._status(f"{len(self._entries)} rule(s)")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-bar":
            self.action_pick()

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> str | None:
        table = self.query_one("#rules-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_pick(self) -> None:
        name = self._selected()
        if name is None:
            self._status("no rule selected")
            return
        self.dismiss(RulePick(name=name))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self.action_pick()
        else:
            self.action_cancel()

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
