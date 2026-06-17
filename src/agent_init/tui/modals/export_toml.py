"""Modal: export a project profile to a TOML file path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from agent_init.core import profiles


@dataclass(frozen=True)
class ExportTomlResult:
    path: Path


class ExportTomlModal(ModalScreen[ExportTomlResult | None]):
    BINDINGS = [
        Binding("escape", "action_cancel", "Cancel", priority=True),
        Binding("enter", "action_export", "Export", priority=True),
    ]

    def __init__(self, profile: profiles.Profile, *, initial_path: str = "") -> None:
        super().__init__()
        self._profile = profile
        self._initial_path = initial_path

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Export template as TOML", classes="modal-title", markup=False),
            Static("Destination path:", markup=False),
            Input(value=self._initial_path, id="path"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Export", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    def action_export(self) -> None:
        value = self.query_one("#path", Input).value.strip()
        if not value:
            self._error("path is required")
            return
        path = Path(value).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(profiles.render_toml(self._profile), encoding="utf-8")
        except Exception as exc:
            self._error(f"export failed: {exc}")
            return
        self.dismiss(ExportTomlResult(path=path))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self.action_export()
        else:
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "path":
            self.action_export()

    def _error(self, msg: str) -> None:
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Export TOML")
