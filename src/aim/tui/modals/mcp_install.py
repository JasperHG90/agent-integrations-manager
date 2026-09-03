"""Modal: configure an MCP server install.

Shows the mapped .mcp.json entry and lets the user set the project root,
local alias, preferred transport, and simple overrides.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from aim.core import mcp_registry
from aim.tui.widgets import ToggleRow


@dataclass(frozen=True)
class McpInstallConfig:
    """Hold the user-selected options for installing an MCP server."""

    project_root: Path
    alias: str
    transport: str | None
    overrides: dict[str, object] | None
    force: bool


class McpInstallModal(ModalScreen[McpInstallConfig | None]):
    """Modal for configuring and confirming an MCP server install."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        ("b", "cancel", "Back"),
        Binding("enter", "submit", "Install", priority=True),
    ]

    def __init__(
        self,
        server: mcp_registry.McpServer,
        *,
        editable: bool = True,
        initial_project: Path | None = None,
        initial_alias: str | None = None,
    ) -> None:
        """Initialize the modal with the server and optional install defaults.

        Args:
            server: The MCP server registry entry being installed.
            editable: Whether the form accepts input or is view-only.
            initial_project: Pre-filled project root; defaults to the cwd.
            initial_alias: Pre-filled local alias; defaults to a name-derived alias.
        """
        super().__init__()
        self._server = server
        self._editable = editable
        self._initial_project = initial_project or Path.cwd()
        self._initial_alias = initial_alias or self._default_alias(server.name)

    @staticmethod
    def _default_alias(name: str) -> str:
        """Derive a filesystem-safe local alias from a server name.

        Args:
            name: The full MCP server name, possibly namespaced.

        Returns:
            A lowercase alias containing only alphanumerics, hyphens, and underscores.
        """
        short = name.split("/")[-1]
        short = short.split(":")[0]
        return "".join(c if c.isalnum() or c in "_-" else "-" for c in short).lower()

    def compose(self) -> ComposeResult:
        """Build the modal layout with the entry preview and install form."""
        try:
            entry = mcp_registry.map_to_claude_entry(self._server)
            entry_json = entry.model_dump_json(exclude_none=True, indent=2)
        except mcp_registry.McpMappingError as exc:
            entry_json = f"(could not map to .mcp.json entry: {exc})"

        yield Vertical(
            Static(f"MCP server: {self._server.name}", classes="modal-title", markup=False),
            VerticalScroll(
                Static(
                    f"version: {self._server.version or '?'}    transport: {(self._server.remotes[0].type if self._server.remotes else 'stdio')}",
                    markup=False,
                ),
                Static("Mapped .mcp.json entry:", markup=False),
                Static(entry_json, id="entry-preview", markup=False),
                Static("Project root:", markup=False),
                Input(value=str(self._initial_project), id="project-root"),
                Static("Local alias:", markup=False),
                Input(value=self._initial_alias, id="alias"),
                Static("Preferred transport (optional):", markup=False),
                Select(
                    [(t, t) for t in ("stdio", "http", "sse", "ws")],
                    allow_blank=True,
                    id="transport",
                ),
                Static("Override command (optional):", markup=False),
                Input(placeholder="npx", id="command"),
                Static(
                    "Override args (optional, shell-style; quote to keep spaces):", markup=False
                ),
                Input(placeholder="-y @scope/server@latest", id="args"),
                Static("Override URL (optional):", markup=False),
                Input(placeholder="https://…", id="url"),
                Horizontal(
                    ToggleRow("Force overwrite", id="force"),
                    classes="modal-checkbox",
                ),
                Static("", id="error", markup=False, classes="modal-error"),
                classes="modal-scroll",
            ),
            Horizontal(
                Button("Install", id="go", variant="primary")
                if self._editable
                else Button("Close", id="go"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the alias input when the modal is mounted."""
        self.query_one("#alias", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss on cancel/close, otherwise submit the install form."""
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if not self._editable:
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit the form when a relevant text input is confirmed."""
        if event.input.id in ("project-root", "alias", "command", "args", "url"):
            self._submit()

    def action_submit(self) -> None:
        """Submit the form, or dismiss when the modal is view-only."""
        if not self._editable:
            self.dismiss(None)
            return
        self._submit()

    def _submit(self) -> None:
        """Validate the form and dismiss with the assembled install config.

        Shows an inline error and aborts when the project root or alias is empty.
        """
        project = self.query_one("#project-root", Input).value.strip()
        alias = self.query_one("#alias", Input).value.strip()
        if not project:
            self._error("project root is required")
            return
        if not alias:
            self._error("alias is required")
            return
        transport = self.query_one("#transport", Select).value
        transport = (
            transport
            if isinstance(transport, str) and transport not in (Select.BLANK, Select.NULL)
            else None
        )
        if isinstance(transport, str):
            transport = transport.strip() or None
        command = self.query_one("#command", Input).value.strip() or None
        args_raw = self.query_one("#args", Input).value.strip()
        args: list[str] | None = None
        if args_raw:
            try:
                args = shlex.split(args_raw)
            except ValueError as exc:
                self._error(f"invalid args: {exc}")
                return
        url = self.query_one("#url", Input).value.strip() or None
        force = self.query_one("#force", ToggleRow).value
        overrides: dict[str, object] = {}
        if command:
            overrides["command"] = command
        if args:
            overrides["args"] = args
        if url:
            overrides["url"] = url
        self.dismiss(
            McpInstallConfig(
                project_root=Path(project).expanduser(),
                alias=alias,
                transport=transport,
                overrides=overrides or None,
                force=force,
            )
        )

    def _error(self, msg: str) -> None:
        """Display a validation error inline and as an app notification.

        Args:
            msg: The error message to surface to the user.
        """
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Install")

    def action_cancel(self) -> None:
        """Dismiss the modal without producing an install config."""
        self.dismiss(None)
