"""Modal: pick an indexed plugin by searching across registered repos."""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from aim.core import plugins


@dataclass(frozen=True)
class PluginPick:
    """Result of the picker: the qualified name and flavor of the chosen plugin."""

    qualified_name: str
    flavor: str


class _PickerDataTable(DataTable):
    """DataTable that forwards Enter to the picker's pick action."""

    def on_key(self, event: events.Key) -> None:
        """Trigger the picker's pick action on Enter, else defer to the base handler."""
        if event.key == "enter":
            event.stop()
            screen = self.screen
            assert isinstance(screen, PluginPickerModal)
            screen.action_pick()
        else:
            super().on_key(event)  # type: ignore[misc]


class PluginPickerModal(ModalScreen[PluginPick | None]):
    """Modal screen for searching indexed plugins and picking one to add."""

    BINDINGS = [
        Binding("escape", "action_cancel", "Cancel", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("enter", "action_pick", "Pick", priority=True),
    ]

    def compose(self) -> ComposeResult:
        """Build the modal layout: title, search bar, results table, and buttons."""
        yield Vertical(
            Static("Add plugin", classes="modal-title", markup=False),
            Input(placeholder="search…", id="search-bar"),
            _PickerDataTable(id="plugins-table", cursor_type="row"),
            Static("", id="status", markup=False),
            Horizontal(
                Button("Add", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Set up table columns, populate all plugins, and focus the search bar."""
        table = self.query_one("#plugins-table", DataTable)
        table.add_columns("qualified name", "flavor", "description")
        self._populate("")
        self.query_one("#search-bar", Input).focus()

    def _populate(self, query: str) -> None:
        """Refill the results table with plugins matching the query.

        Args:
            query: Search term; when empty, lists all indexed plugins.
        """
        table = self.query_one("#plugins-table", DataTable)
        table.clear()
        rows = plugins.search(query) if query else plugins.list_plugins()
        if not rows:
            self._status("no plugins indexed — add a repo with a marketplace first")
            return
        for r in rows:
            table.add_row(
                r.qualified_name,
                r.flavor,
                (r.description or "")[:60],
            )
        self._status(f"{len(rows)} plugin(s)")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-run the search as the user types in the search bar."""
        if event.input.id == "search-bar":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Pick the selected plugin when the search bar is submitted."""
        if event.input.id == "search-bar":
            self.action_pick()

    def action_focus_search(self) -> None:
        """Move keyboard focus to the search bar."""
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> tuple[str, str] | None:
        """Return (qualified_name, flavor) under the cursor, or None when empty."""
        table = self.query_one("#plugins-table", DataTable)
        if table.row_count == 0:
            return None
        row = table.get_row_at(table.cursor_coordinate.row)
        return str(row[0]), str(row[1])

    def action_pick(self) -> None:
        """Dismiss with the selected plugin, or show a status when nothing is selected."""
        picked = self._selected()
        if picked is None:
            self._status("no plugin selected")
            return
        self.dismiss(PluginPick(qualified_name=picked[0], flavor=picked[1]))

    def action_cancel(self) -> None:
        """Dismiss the modal without selecting a plugin."""
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Pick on the Add button, otherwise cancel the modal."""
        if event.button.id == "go":
            self.action_pick()
        else:
            self.action_cancel()

    def _status(self, msg: str) -> None:
        """Update the modal's status line with the given message."""
        self.query_one("#status", Static).update(msg)
