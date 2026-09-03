"""Modal: read a sub-agent's AGENT.md content."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class AgentViewModal(ModalScreen[None]):
    """Modal screen that displays a sub-agent's AGENT.md content read-only."""

    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, qualified_name: str, content: str) -> None:
        """Initialize the modal with the agent's name and AGENT.md content.

        Args:
            qualified_name: Fully qualified name of the sub-agent, shown as the title.
            content: Raw AGENT.md text to display in the read-only body.
        """
        super().__init__()
        self._qualified_name = qualified_name
        self._content = content

    def compose(self) -> ComposeResult:
        """Build the modal layout with title, body, and close button."""
        yield Vertical(
            Static(f"{self._qualified_name}", classes="modal-title", markup=False),
            TextArea(self._content, id="agent-body", read_only=True),
            Button("Close", id="close"),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the agent body text area when the modal is mounted."""
        self.query_one("#agent-body", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss the modal when the close button is pressed."""
        if event.button.id == "close":
            self.dismiss(None)

    def action_close(self) -> None:
        """Dismiss the modal."""
        self.dismiss(None)
