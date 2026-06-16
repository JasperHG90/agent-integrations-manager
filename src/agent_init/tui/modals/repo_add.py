"""Modal: register a new skill source repository."""

from __future__ import annotations

import re
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from agent_init.tui.widgets import ToggleRow


def sanitize_repo_alias(raw: str) -> str:
    """Coerce a user-typed alias into `[a-z0-9][a-z0-9_-]*` shape."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"^[^a-z0-9]+", "", s)
    s = re.sub(r"[-_]+$", "", s)
    return s


@dataclass(frozen=True)
class RepoAddResult:
    alias: str
    url: str
    default_ref: str
    allow_empty: bool


class RepoAddModal(ModalScreen[RepoAddResult | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Add", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Add skill / agent repository", classes="modal-title", markup=False),
            Static("Alias (lowercase, [a-z0-9_-]):", markup=False),
            Input(placeholder="anthropic", id="alias"),
            Static("Git URL (https / ssh / file://):", markup=False),
            Input(placeholder="https://github.com/anthropics/skills", id="url"),
            Static("Default ref (branch or tag):", markup=False),
            Input(value="HEAD", id="default-ref"),
            ToggleRow("Allow registering even if no skills or agents found", id="allow-empty"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Add", id="add", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#alias", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("alias", "url", "default-ref"):
            self._submit()

    def action_submit(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _error(self, msg: str, focus_id: str) -> None:
        self.query_one("#error", Static).update(msg)
        self.query_one(f"#{focus_id}", Input).focus()
        self.app.notify(msg, severity="error", title="Add repo")

    def _submit(self) -> None:
        raw_alias = self.query_one("#alias", Input).value
        url = self.query_one("#url", Input).value.strip()
        default_ref = self.query_one("#default-ref", Input).value.strip() or "HEAD"
        allow_empty = self.query_one("#allow-empty", ToggleRow).value
        alias = sanitize_repo_alias(raw_alias)
        if not alias:
            alias_input = self.query_one("#alias", Input)
            alias_input.value = alias
            self._error("alias is required (lowercase letters/numbers/_-)", "alias")
            return
        if not url:
            self._error("repo URL is required", "url")
            return
        self.dismiss(
            RepoAddResult(
                alias=alias, url=url, default_ref=default_ref, allow_empty=allow_empty
            )
        )
