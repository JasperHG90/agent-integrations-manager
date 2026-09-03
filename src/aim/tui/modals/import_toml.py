"""Modal: import a project profile from a TOML file path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from aim.core import profiles


@dataclass(frozen=True)
class ImportTomlResult:
    """Outcome of a successful TOML import: the parsed profile and its source path."""

    profile: profiles.Profile
    path: Path


class ImportTomlModal(ModalScreen[ImportTomlResult | None]):
    """Modal prompting for a TOML path and importing it as a project profile."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "action_load", "Load", priority=True),
    ]

    def __init__(self, *, initial_path: str = "") -> None:
        """Initialize the modal, optionally pre-filling the path input.

        Args:
            initial_path: Value to seed the path input with.
        """
        super().__init__()
        self._initial_path = initial_path

    def compose(self) -> ComposeResult:
        """Build the modal's widget tree."""
        yield Vertical(
            Static("Import template from TOML", classes="modal-title", markup=False),
            Static("Path to TOML file:", markup=False),
            Input(value=self._initial_path, id="path"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Load", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the path input when the modal is mounted."""
        self.query_one("#path", Input).focus()

    def action_load(self) -> None:
        """Read and parse the entered TOML path, dismissing with the result or an error."""
        value = self.query_one("#path", Input).value.strip()
        if not value:
            self._error("path is required")
            return
        path = Path(value).expanduser()
        if not path.exists():
            self._error(f"file not found: {path}")
            return
        try:
            text = path.read_text(encoding="utf-8")
            profile = profiles.parse_toml(text, source=str(path))
        except profiles.ProfileTomlError as exc:
            self._error(str(exc))
            return
        except Exception as exc:
            self._error(f"import failed: {exc}")
            return
        self.dismiss(ImportTomlResult(profile=profile, path=path))

    def action_cancel(self) -> None:
        """Dismiss the modal without a result."""
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to the load or cancel action."""
        if event.button.id == "go":
            self.action_load()
        else:
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Trigger loading when the path input is submitted."""
        if event.input.id == "path":
            self.action_load()

    def _error(self, msg: str) -> None:
        """Display an error message in the modal and as an app notification.

        Args:
            msg: The error text to show.
        """
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Import TOML")
