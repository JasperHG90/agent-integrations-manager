"""Modal: read a skill's SKILL.md content."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class SkillViewModal(ModalScreen[None]):
    """Display a skill's SKILL.md content in a read-only modal."""

    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, qualified_name: str, content: str) -> None:
        """Initialize the modal with a skill's name and rendered content.

        Args:
            qualified_name: Fully qualified skill name shown in the title.
            content: SKILL.md text displayed in the read-only body.
        """
        super().__init__()
        self._qualified_name = qualified_name
        self._content = content

    def compose(self) -> ComposeResult:
        """Build the modal layout with a title, body, and close button."""
        yield Vertical(
            Static(f"{self._qualified_name}", classes="modal-title", markup=False),
            TextArea(self._content, id="skill-body", read_only=True),
            Button("Close", id="close"),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the skill body when the modal is mounted."""
        self.query_one("#skill-body", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss the modal when the close button is pressed."""
        if event.button.id == "close":
            self.dismiss(None)

    def action_close(self) -> None:
        """Dismiss the modal."""
        self.dismiss(None)
