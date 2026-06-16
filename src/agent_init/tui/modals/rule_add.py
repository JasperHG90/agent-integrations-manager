"""Modal: add a new rule to the global library."""

from __future__ import annotations

import re
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from agent_init.tui.widgets import ToggleRow


def sanitize_rule_name(raw: str) -> str:
    """Coerce a user-typed name into the canonical rule-name shape."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"^[^a-z0-9]+", "", s)
    s = re.sub(r"[-_]+$", "", s)
    return s


@dataclass(frozen=True)
class RuleAddResult:
    name: str
    body: str
    description: str | None
    is_default: bool


class RuleAddModal(ModalScreen[RuleAddResult | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Save", priority=True),
    ]

    def __init__(self, existing_body: str = "", existing_name: str = "",
                 existing_description: str = "", existing_default: bool = False,
                 is_edit: bool = False) -> None:
        super().__init__()
        self._initial_body = existing_body
        self._initial_name = existing_name
        self._initial_description = existing_description
        self._initial_default = existing_default
        self._is_edit = is_edit

    def compose(self) -> ComposeResult:
        title = "Edit rule" if self._is_edit else "Add rule"
        yield Vertical(
            Static(title, classes="modal-title", markup=False),
            Static("Name:", markup=False),
            Input(value=self._initial_name, placeholder="be-concise", id="name"),
            Static("Description (optional):", markup=False),
            Input(value=self._initial_description, placeholder="short reminder", id="description"),
            Static("Body (Ctrl+S to save):", markup=False),
            TextArea(text=self._initial_body, id="body", classes="rule-body"),
            ToggleRow("Default — auto-seed into `init`", value=self._initial_default, id="default"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Save", id="save", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        if not self._initial_name:
            self.query_one("#name", Input).focus()
        else:
            self.query_one("#body", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _error(self, msg: str, *, focus_name: bool = False) -> None:
        self.query_one("#error", Static).update(msg)
        if focus_name:
            self.query_one("#name", Input).focus()
        self.app.notify(msg, severity="error", title="Save rule")

    def action_submit(self) -> None:
        raw_name = self.query_one("#name", Input).value
        body = self.query_one("#body", TextArea).text
        description = self.query_one("#description", Input).value.strip() or None
        is_default = self.query_one("#default", ToggleRow).value
        name = sanitize_rule_name(raw_name)
        # Reflect the sanitized name back into the field so user sees the change.
        name_input = self.query_one("#name", Input)
        name_input.value = name
        if not name:
            self._error("rule name is required", focus_name=True)
            return
        if not body.strip():
            self._error("rule body is required")
            self.query_one("#body", TextArea).focus()
            return
        self.dismiss(
            RuleAddResult(
                name=name, body=body, description=description, is_default=is_default
            )
        )

    @property
    def is_edit(self) -> bool:
        return self._is_edit
