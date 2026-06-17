"""Textual app shell. The TUI is a thin layer over `core/`; everything here
should route through the core API so CLI and TUI stay in sync.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from aim.core import default_mcp_servers, layout_profiles, mcp_registry
from aim.tui.modals.palette import (
    PaletteEntry,
    PaletteModal,
    build_entries,
)
from aim.tui.screens.main_screen import MainScreen


class AimApp(App[None]):
    """Top-level Textual app."""

    TITLE = "aim"
    SUB_TITLE = "scaffold and manage agent-engineering projects"
    CSS_PATH = "app.tcss"

    # NOTE: action name is `open_palette` (not `command_palette`) to avoid
    # collision with Textual's built-in app command palette.
    BINDINGS = [
        ("q", "quit", "Quit"),
        Binding("ctrl+p", "open_palette", "Palette", priority=True),
    ]

    def __init__(
        self,
        project_root: Path | None = None,
        profile_name: str | None = None,
    ) -> None:
        super().__init__()
        self._project_root = (project_root or Path.cwd()).expanduser().resolve()
        self._profile_name = profile_name

    def on_mount(self) -> None:
        report = layout_profiles.sync_profiles(self._project_root)
        for warning in report.warnings:
            self.app.notify(warning, severity="warning")

        # Pre-seed default MCP registry entries in the background so the MCP
        # screen opens instantly from cache instead of blocking on the network.
        self.run_worker(self._seed_default_mcp_servers, group="mcp_seed", thread=True)

        if self._profile_name:
            try:
                layout_profiles.set_active(self._project_root, self._profile_name)
            except Exception as exc:
                self.app.notify(
                    f"profile {self._profile_name!r} not applied: {exc}", severity="warning"
                )

        self.push_screen(MainScreen(project_root=self._project_root))

    def _seed_default_mcp_servers(self) -> None:
        try:
            mcp_registry.seed_default_servers(default_mcp_servers.DEFAULT_MCP_SERVER_NAMES)
        except Exception:
            # Best-effort startup seeding; the MCP screen retries on open.
            pass

    def action_open_palette(self) -> None:
        entries = build_entries(self)
        self.push_screen(PaletteModal(entries), self._on_palette)

    def _on_palette(self, entry: PaletteEntry | None) -> None:
        if entry is None:
            return
        entry.handler()


def run(project_root: Path | None = None, profile_name: str | None = None) -> None:
    AimApp(project_root=project_root, profile_name=profile_name).run()
