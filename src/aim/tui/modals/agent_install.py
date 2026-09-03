"""Modal: pick the project root to install a sub-agent into."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from aim.tui.widgets import ToggleRow


@dataclass(frozen=True)
class AgentInstallConfig:
    """Capture the user's chosen target and ref pinning for an install."""

    project_root: Path
    pin: str | None = None
    track: str | None = None
    override_risk: bool = False


class AgentInstallModal(ModalScreen[AgentInstallConfig | None]):
    """Modal that prompts for the project root and optional ref to install into."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Install", priority=True),
    ]

    def __init__(self, qualified_name: str, *, initial_project: Path | None = None) -> None:
        """Initialize the modal for a given sub-agent.

        Args:
            qualified_name: Fully qualified name of the sub-agent to install.
            initial_project: Pre-filled project root; defaults to the current directory.
        """
        super().__init__()
        self._qualified_name = qualified_name
        self._initial_project = initial_project or Path.cwd()

    def compose(self) -> ComposeResult:
        """Build the modal's widget tree."""
        yield Vertical(
            Static(f"Install {self._qualified_name}", classes="modal-title", markup=False),
            VerticalScroll(
                Static("Project root (will be created if missing):", markup=False),
                Input(value=str(self._initial_project), id="project-root"),
                Static("Pin to ref (tag/sha/branch) — optional:", markup=False),
                Input(value="", id="pin", placeholder="e.g. v1.2.3"),
                Static("Track ref (branch or 'latest-tag') — optional:", markup=False),
                Input(value="", id="track", placeholder="e.g. main or latest-tag"),
                ToggleRow("Override risk gate (--override-risk)", id="override-risk"),
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
        """Focus the project-root input when the modal mounts."""
        self.query_one("#project-root", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Submit on the install button, otherwise dismiss without a config."""
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit when the project-root input is confirmed."""
        if event.input.id == "project-root":
            self._submit()

    def action_submit(self) -> None:
        """Handle the bound submit action."""
        self._submit()

    def _submit(self) -> None:
        """Validate inputs and dismiss with an install config, or report an error."""
        value = self.query_one("#project-root", Input).value.strip()
        if not value:
            self.query_one("#error", Static).update("project root is required")
            self.app.notify("project root is required", severity="error", title="Install")
            self.query_one("#project-root", Input).focus()
            return
        pin = self.query_one("#pin", Input).value.strip() or None
        track = self.query_one("#track", Input).value.strip() or None
        override_risk = self.query_one("#override-risk", ToggleRow).value
        self.dismiss(
            AgentInstallConfig(
                project_root=Path(value).expanduser(),
                pin=pin,
                track=track,
                override_risk=override_risk,
            )
        )

    def action_cancel(self) -> None:
        """Dismiss the modal without producing a config."""
        self.dismiss(None)
