"""Modal: pick the project root and options to install a plugin into."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static


@dataclass(frozen=True)
class PluginInstallConfig:
    """Capture the user's choices for installing a plugin into a project."""

    project_root: Path
    pin: str | None = None
    track: str | None = None
    override_risk: bool = False


class PluginInstallModal(ModalScreen[PluginInstallConfig | None]):
    """Prompt for the project root, ref options, and risk override for a plugin."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Install", priority=True),
    ]

    def __init__(self, qualified_name: str, *, initial_project: Path | None = None) -> None:
        """Initialize the modal for the given plugin.

        Args:
            qualified_name: Fully qualified name of the plugin to install.
            initial_project: Pre-filled project root; defaults to the current directory.
        """
        super().__init__()
        self._qualified_name = qualified_name
        self._initial_project = initial_project or Path.cwd()

    def compose(self) -> ComposeResult:
        """Build the modal layout."""
        yield Vertical(
            Static(f"Install {self._qualified_name}", classes="modal-title", markup=False),
            VerticalScroll(
                Static("Project root (will be created if missing):", markup=False),
                Input(value=str(self._initial_project), id="project-root"),
                Static("Pin to a git ref (tag/sha) — optional:", markup=False),
                Input(value="", id="pin", placeholder="e.g. v1.2.3 or a short sha from the list"),
                Static("Track ref (branch or 'latest-tag') — optional:", markup=False),
                Input(value="", id="track", placeholder="e.g. main or latest-tag"),
                Checkbox("Override risk gate (--override-risk)", id="override-risk"),
                Static("", id="error", markup=False, classes="modal-error"),
                classes="modal-scroll",
            ),
            Horizontal(
                Button("Install", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the project root input when the modal mounts."""
        self.query_one("#project-root", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Submit on the install button, otherwise dismiss without a result."""
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit when the project root input is confirmed with Enter."""
        if event.input.id == "project-root":
            self._submit()

    def action_submit(self) -> None:
        """Handle the submit binding."""
        self._submit()

    def _submit(self) -> None:
        """Validate inputs and dismiss with a config, or surface an error."""
        value = self.query_one("#project-root", Input).value.strip()
        if not value:
            self.query_one("#error", Static).update("project root is required")
            self.app.notify("project root is required", severity="error", title="Install")
            self.query_one("#project-root", Input).focus()
            return
        pin = self.query_one("#pin", Input).value.strip() or None
        track = self.query_one("#track", Input).value.strip() or None
        override_risk = self.query_one("#override-risk", Checkbox).value
        self.dismiss(
            PluginInstallConfig(
                project_root=Path(value).expanduser(),
                pin=pin,
                track=track,
                override_risk=override_risk,
            )
        )

    def action_cancel(self) -> None:
        """Dismiss the modal without a result."""
        self.dismiss(None)

    def on_key(self, event) -> None:
        """Intercept Escape so it cancels the modal instead of bubbling up."""
        if event.key == "escape":
            event.stop()
            self.action_cancel()
