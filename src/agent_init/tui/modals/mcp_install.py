"""Modal: configure an MCP server install.

Shows the mapped .mcp.json entry and lets the user set the project root,
local alias, preferred transport, and simple overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from agent_init.core import mcp_registry
from agent_init.tui.widgets import ToggleRow


@dataclass(frozen=True)
class McpInstallConfig:
    project_root: Path
    alias: str
    transport: str | None
    overrides: dict[str, object] | None
    force: bool


class McpInstallModal(ModalScreen[McpInstallConfig | None]):
    BINDINGS = [
        Binding("escape", "action_cancel", "Cancel", priority=True),
        ("b", "action_cancel", "Back"),
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
        super().__init__()
        self._server = server
        self._editable = editable
        self._initial_project = initial_project or Path.cwd()
        self._initial_alias = initial_alias or self._default_alias(server.name)

    @staticmethod
    def _default_alias(name: str) -> str:
        # Take last segment, drop namespace-ish dots.
        short = name.split("/")[-1]
        short = short.split(":")[0]
        return "".join(c if c.isalnum() or c in "_-" else "-" for c in short).lower()

    def compose(self) -> ComposeResult:
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
                Button("Install", id="go", variant="primary") if self._editable else Button("Close", id="go"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#alias", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if not self._editable:
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("project-root", "alias", "command", "url"):
            self._submit()

    def action_submit(self) -> None:
        if not self._editable:
            self.dismiss(None)
            return
        self._submit()

    def _submit(self) -> None:
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
        url = self.query_one("#url", Input).value.strip() or None
        force = self.query_one("#force", ToggleRow).value
        overrides: dict[str, object] = {}
        if command:
            overrides["command"] = command
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
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Install")

    def action_cancel(self) -> None:
        self.dismiss(None)
