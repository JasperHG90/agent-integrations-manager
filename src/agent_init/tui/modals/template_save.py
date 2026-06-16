"""Modal: save the current project as a reusable project template."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


@dataclass(frozen=True)
class TemplateSaveResult:
    name: str


class TemplateSaveModal(ModalScreen[TemplateSaveResult | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Save", priority=True),
    ]

    def __init__(self, *, initial_name: str = "") -> None:
        super().__init__()
        self._initial_name = initial_name

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Save project as template", classes="modal-title", markup=False),
            Static("Template name:", markup=False),
            Input(value=self._initial_name, id="name"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Save", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        self._submit()

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._error("template name is required")
            return
        self.dismiss(TemplateSaveResult(name=name))

    def _error(self, msg: str) -> None:
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Save template")

    def action_cancel(self) -> None:
        self.dismiss(None)
