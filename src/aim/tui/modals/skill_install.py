"""Modal: pick the project root to install a skill into."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


@dataclass(frozen=True)
class SkillInstallConfig:
    project_root: Path
    pin: str | None = None
    track: str | None = None


class SkillInstallModal(ModalScreen[SkillInstallConfig | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Install", priority=True),
    ]

    def __init__(self, qualified_name: str, *, initial_project: Path | None = None) -> None:
        super().__init__()
        self._qualified_name = qualified_name
        self._initial_project = initial_project or Path.cwd()

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"Install {self._qualified_name}", classes="modal-title", markup=False),
            VerticalScroll(
                Static("Project root (will be created if missing):", markup=False),
                Input(value=str(self._initial_project), id="project-root"),
                Static("Pin to ref (tag/sha/branch) — optional:", markup=False),
                Input(value="", id="pin", placeholder="e.g. v1.2.3"),
                Static("Track ref (branch or 'latest-tag') — optional:", markup=False),
                Input(value="", id="track", placeholder="e.g. main or latest-tag"),
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
        self.query_one("#project-root", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "project-root":
            self._submit()

    def action_submit(self) -> None:
        self._submit()

    def _submit(self) -> None:
        value = self.query_one("#project-root", Input).value.strip()
        if not value:
            self.query_one("#error", Static).update("project root is required")
            self.app.notify("project root is required", severity="error", title="Install")
            self.query_one("#project-root", Input).focus()
            return
        pin = self.query_one("#pin", Input).value.strip() or None
        track = self.query_one("#track", Input).value.strip() or None
        self.dismiss(
            SkillInstallConfig(
                project_root=Path(value).expanduser(),
                pin=pin,
                track=track,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.action_cancel()
