"""Generic project-root picker modal — reused by skill install, rule install,
and anywhere else the TUI needs the user to pick a target project directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


@dataclass(frozen=True)
class ProjectPick:
    project_root: Path


class ProjectPickerModal(ModalScreen[ProjectPick | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(
        self,
        title: str,
        *,
        action_label: str = "Go",
        initial_project: Path | None = None,
        helper: str = "Project root (will be created if missing):",
    ) -> None:
        super().__init__()
        self._title = title
        self._action_label = action_label
        self._initial_project = initial_project or Path.cwd()
        self._helper = helper

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._title, classes="modal-title", markup=False),
            Static(self._helper, markup=False),
            Input(value=str(self._initial_project), id="project-root"),
            Horizontal(
                Button(self._action_label, id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#project-root", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            value = self.query_one("#project-root", Input).value.strip()
            if not value:
                return
            self.dismiss(ProjectPick(project_root=Path(value).expanduser()))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
