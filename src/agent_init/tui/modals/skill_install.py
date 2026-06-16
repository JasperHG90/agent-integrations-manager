"""Modal: pick the project root to install a skill into."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


@dataclass(frozen=True)
class SkillInstallConfig:
    project_root: Path


class SkillInstallModal(ModalScreen[SkillInstallConfig | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "submit", "Install"),
    ]

    def __init__(self, qualified_name: str, *, initial_project: Path | None = None) -> None:
        super().__init__()
        self._qualified_name = qualified_name
        self._initial_project = initial_project or Path.cwd()

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"Install {self._qualified_name}", classes="modal-title", markup=False),
            Static("Project root (will be created if missing):", markup=False),
            Input(value=str(self._initial_project), id="project-root"),
            Static("", id="error", markup=False, classes="modal-error"),
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

    def action_submit(self) -> None:
        self._submit()

    def _submit(self) -> None:
        value = self.query_one("#project-root", Input).value.strip()
        if not value:
            self.query_one("#error", Static).update("project root is required")
            self.app.notify("project root is required", severity="error", title="Install")
            self.query_one("#project-root", Input).focus()
            return
        self.dismiss(SkillInstallConfig(project_root=Path(value).expanduser()))

    def action_cancel(self) -> None:
        self.dismiss(None)
