"""A small yes/no confirmation modal used for destructive actions.

Returns `bool | None` — `None` if the user pressed Escape (cancelled in a way
that didn't explicitly hit either button). Callbacks should compare with
`is True` rather than truthiness so cancellation is always treated as a
no-op.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool | None]):
    """Modal screen that asks the user to confirm a destructive action."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "confirm", "Confirm"),
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
    ]

    def __init__(self, prompt: str, *, confirm_label: str = "Confirm") -> None:
        """Initialize the modal with its prompt and confirm button label.

        Args:
            prompt: Message shown to the user explaining the action.
            confirm_label: Text rendered on the confirm button.
        """
        super().__init__()
        self._prompt = prompt
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        """Build the prompt body and confirm/cancel buttons."""
        yield Vertical(
            VerticalScroll(
                Static(self._prompt, markup=False, classes="modal-body"),
                classes="modal-scroll",
            ),
            Horizontal(
                Button(self._confirm_label, id="confirm", variant="error"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with True when confirm was pressed, otherwise False."""
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        """Dismiss the modal with a confirmed (True) result."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Dismiss the modal with a cancelled (False) result."""
        self.dismiss(False)
