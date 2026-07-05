"""Modal: change the branch/tag a registered repo tracks."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


@dataclass(frozen=True)
class RepoEditRefResult:
    """Result returned when the user confirms a new tracked ref."""

    alias: str
    default_ref: str


class RepoEditRefModal(ModalScreen[RepoEditRefResult | None]):
    """Modal prompting for the branch or tag a repo should track."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Update", priority=True),
    ]

    def __init__(self, alias: str, current_ref: str = "HEAD") -> None:
        """Initialize the modal for `alias`, pre-filled with its current ref.

        Args:
            alias: The repo alias whose tracked ref is being changed.
            current_ref: The ref currently tracked, pre-populated in the input.
        """
        super().__init__()
        self._alias = alias
        self._current_ref = current_ref

    def compose(self) -> ComposeResult:
        """Build the modal's widget tree."""
        yield Vertical(
            Static(f"Set tracked ref for {self._alias!r}", classes="modal-title", markup=False),
            Static("Ref (branch or tag):", markup=False),
            Input(value=self._current_ref, id="ref"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Update", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the ref input when the modal is mounted."""
        self.query_one("#ref", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Submit on the Update button, otherwise dismiss without a result.

        Args:
            event: The button-pressed event identifying which button fired.
        """
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        """Handle the submit binding by attempting to update."""
        self._submit()

    def _submit(self) -> None:
        """Validate the entered ref and dismiss with the result.

        Shows an inline error and keeps the modal open when the ref is empty.
        """
        ref = self.query_one("#ref", Input).value.strip()
        if not ref:
            self._error("ref is required")
            return
        self.dismiss(RepoEditRefResult(alias=self._alias, default_ref=ref))

    def _error(self, msg: str) -> None:
        """Display an error message inline and as a notification.

        Args:
            msg: The error message to surface to the user.
        """
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Set ref")

    def action_cancel(self) -> None:
        """Dismiss the modal without changing the ref."""
        self.dismiss(None)
