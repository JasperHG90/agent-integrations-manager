"""Modal: pick an indexed skill by searching across registered repos."""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from aim.core import skills


@dataclass(frozen=True)
class SkillPick:
    """Result of the picker: the qualified name of the chosen skill."""

    qualified_name: str


class _PickerDataTable(DataTable):
    """DataTable that forwards Enter to the picker's pick action."""

    def on_key(self, event: events.Key) -> None:
        """Trigger the picker's pick action on Enter, else defer to the base handler."""
        if event.key == "enter":
            event.stop()
            screen = self.screen
            assert isinstance(screen, SkillPickerModal)
            screen.action_pick()
        else:
            super().on_key(event)  # type: ignore[misc]


class SkillPickerModal(ModalScreen[SkillPick | None]):
    """Modal screen for searching indexed skills and picking one to add."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("enter", "pick", "Pick", priority=True),
    ]

    def __init__(self) -> None:
        """Initialize the modal with no repo filter applied."""
        super().__init__()
        self._repo_filter: str | None = None

    def compose(self) -> ComposeResult:
        """Build the modal layout: title, search bar, results table, and buttons."""
        yield Vertical(
            Static("Add skill", classes="modal-title", markup=False),
            Input(placeholder="search…", id="search-bar"),
            _PickerDataTable(id="skills-table", cursor_type="row"),
            Static("", id="status", markup=False),
            Horizontal(
                Button("Add", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Set up table columns, populate all skills, and focus the search bar."""
        table = self.query_one("#skills-table", DataTable)
        table.add_columns("qualified name", "title", "description")
        self._populate("")
        self.query_one("#search-bar", Input).focus()

    def _populate(self, query: str) -> None:
        """Refill the results table with skills matching the query and repo filter.

        Args:
            query: Search term; when empty, lists all indexed skills.
        """
        table = self.query_one("#skills-table", DataTable)
        table.clear()
        rows = skills.search(query) if query else skills.list_skills()
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""
        if not rows:
            self._status("no skills indexed — add a repo first" + filter_label)
            return
        for r in rows:
            table.add_row(
                r.qualified_name,
                r.title or "",
                (r.description or "")[:60],
                key=r.qualified_name,
            )
        self._status(f"{len(rows)} skill(s){filter_label}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-run the search as the user types in the search bar."""
        if event.input.id == "search-bar":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Pick the selected skill when the search bar is submitted."""
        if event.input.id == "search-bar":
            self.action_pick()

    def action_focus_search(self) -> None:
        """Move keyboard focus to the search bar."""
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> str | None:
        """Return the qualified name under the cursor, or None if the table is empty."""
        table = self.query_one("#skills-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_pick(self) -> None:
        """Dismiss with the selected skill, or show a status when nothing is selected."""
        qn = self._selected()
        if qn is None:
            self._status("no skill selected")
            return
        self.dismiss(SkillPick(qualified_name=qn))

    def action_cancel(self) -> None:
        """Dismiss the modal without selecting a skill."""
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
