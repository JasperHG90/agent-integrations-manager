"""Textual app shell. The TUI is a thin layer over `core/`; everything here
should route through the core API so CLI and TUI stay in sync.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from agent_init.core import default_mcp_servers, layout_profiles, mcp_registry
from agent_init.tui.modals.layout_profile_picker_modal import (
    LayoutProfilePickerModal,
)
from agent_init.tui.modals.palette import (
    PaletteEntry,
    PaletteModal,
    build_entries,
)
from agent_init.tui.screens.main_screen import MainScreen


class AgentInitApp(App[None]):
    """Top-level Textual app."""

    TITLE = "agent-init"
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
        self.run_worker(
            self._seed_default_mcp_servers, group="mcp_seed", thread=True
        )

        active = self._resolve_active()
        if active is None:
            self.push_screen(
                LayoutProfilePickerModal(self._project_root),
                self._on_profile_picked,
            )
        else:
            self.push_screen(MainScreen(project_root=self._project_root))

    def _seed_default_mcp_servers(self) -> None:
        try:
            mcp_registry.seed_default_servers(
                default_mcp_servers.DEFAULT_MCP_SERVER_NAMES
            )
        except Exception:
            # Best-effort startup seeding; the MCP screen retries on open.
            pass

    def _resolve_active(self) -> str | None:
        candidates: list[str | None] = [
            self._profile_name,
        ]
        try:
            profile = layout_profiles.resolve_active(self._project_root)
        except Exception:
            profile = None
        if profile is not None and profile.name != layout_profiles.LEGACY_PROFILE.name:
            candidates.append(profile.name)
        if profile is None or profile.name == layout_profiles.LEGACY_PROFILE.name:
            candidates.append(layout_profiles.get_global_default())
        for name in candidates:
            if not name:
                continue
            try:
                layout_profiles.get_profile(self._project_root, name)
            except layout_profiles.LayoutProfileNotFoundError:
                self.app.notify(
                    f"layout profile {name!r} not found; pick one", severity="warning"
                )
                continue
            return name
        return None

    def _on_profile_picked(self, result: tuple[str, bool] | None) -> None:
        if result is None:
            self.app.notify("no profile selected; using legacy layout", severity="warning")
            self.push_screen(MainScreen(project_root=self._project_root))
            return
        name, remember = result
        layout_profiles.set_active(self._project_root, name)
        if remember:
            layout_profiles.set_global_default(name)
        self.push_screen(MainScreen(project_root=self._project_root))

    def action_open_palette(self) -> None:
        entries = build_entries(self)
        self.push_screen(PaletteModal(entries), self._on_palette)

    def _on_palette(self, entry: PaletteEntry | None) -> None:
        if entry is None:
            return
        entry.handler()


def run(project_root: Path | None = None, profile_name: str | None = None) -> None:
    AgentInitApp(project_root=project_root, profile_name=profile_name).run()
