"""Modal: read a sub-agent's AGENT.md content."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class AgentViewModal(ModalScreen[None]):
    BINDINGS = [("escape", "action_close", "Close")]

    def __init__(self, qualified_name: str, content: str) -> None:
        super().__init__()
        self._qualified_name = qualified_name
        self._content = content

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"{self._qualified_name}", classes="modal-title", markup=False),
            TextArea(self._content, id="agent-body", read_only=True),
            Button("Close", id="close"),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#agent-body", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
