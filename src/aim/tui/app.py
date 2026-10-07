"""Textual app shell. The TUI is a thin layer over `core/`; everything here
should route through the core API so CLI and TUI stay in sync.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.notifications import SeverityLevel

from aim.core import default_mcp_servers, layout_profiles, mcp_registry
from aim.tui import _threads
from aim.tui.modals.palette import (
    PaletteEntry,
    PaletteModal,
    build_entries,
)
from aim.tui.screens.main_screen import MainScreen


class AimApp(App[None]):
    """Provide the top-level Textual app shell for aim."""

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
        """Initialize the app with a resolved project root and optional profile.

        Args:
            project_root: Directory the TUI operates on; defaults to the cwd.
            profile_name: Layout profile to activate on mount, if any.
        """
        super().__init__()
        self._project_root = (project_root or Path.cwd()).expanduser().resolve()
        self._profile_name = profile_name

    def on_mount(self) -> None:
        """Activate any requested profile, push the main screen, then sync in the background.

        Profile sync and MCP seeding both touch the DB (which lazily runs schema
        migrations on first use), so they run off the paint path to keep startup
        responsive. The `--profile` activation stays synchronous because the main screen
        reads the active profile when it composes its banner.
        """
        if self._profile_name:
            try:
                layout_profiles.set_active(self._project_root, self._profile_name)
            except Exception as exc:
                self.notify(
                    f"profile {self._profile_name!r} not applied: {exc}", severity="warning"
                )

        self.push_screen(MainScreen(project_root=self._project_root))

        self.run_worker(self._sync_profiles, group="profile_sync", thread=True)
        # Pre-seed default MCP registry entries so the MCP screen opens from
        # cache. A DAEMON thread, deliberately NOT a Textual worker: worker
        # threads are joined at interpreter exit, so a thread blocked in an
        # HTTP fetch (10s retry budget per request) stalled quitting until the
        # registry round-trips finished. A daemon thread dies with the process;
        # the sqlite cache is WAL crash-safe, and the MCP screen re-seeds on
        # open anyway (also detached — see aim.tui._threads).
        _threads.run_detached(self._seed_default_mcp_servers, name="mcp-seed")

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        """Post a toast, treating the message as plain text unless told otherwise.

        Textual parses toast text as markup by default, so exception text that
        contains brackets (Pydantic's `[type=..., input_value=...]`, TOML errors,
        git stderr) raises `MarkupError` while rendering and crashes the app.
        Screens and modals must call `self.app.notify`, never their own
        `Widget.notify`: that one forwards `markup=True` explicitly and bypasses
        this default.

        Args:
            message: Text to show.
            title: Optional toast title.
            severity: Toast severity.
            timeout: Seconds to show the toast; Textual's default when None.
            markup: Parse `message` as Textual markup. Off by default.
        """
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)

    def _sync_profiles(self) -> None:
        """Reconcile repo profiles with the DB cache, surfacing warnings on the UI thread."""
        report = layout_profiles.sync_profiles(self._project_root)
        for warning in report.warnings:
            self.call_from_thread(self.notify, warning, severity="warning")

    def _seed_default_mcp_servers(self) -> None:
        """Seed the default MCP registry entries on a best-effort basis."""
        try:
            mcp_registry.seed_default_servers(default_mcp_servers.DEFAULT_MCP_SERVER_NAMES)
        except Exception:
            # Best-effort startup seeding; the MCP screen retries on open.
            pass

    def action_open_palette(self) -> None:
        """Open the command palette modal.

        Refused while a busy overlay is on top: anything stacked above it would
        be popped by the overlay's dismiss (Screen.dismiss pops the TOP screen),
        stranding the overlay with no bindings to escape it.
        """
        from aim.tui.modals.busy import BusyModal

        if isinstance(self.screen, BusyModal):
            return
        entries = build_entries(self)
        self.push_screen(PaletteModal(entries), self._on_palette)

    def _on_palette(self, entry: PaletteEntry | None) -> None:
        """Invoke the selected palette entry's handler, ignoring dismissal.

        Args:
            entry: The chosen palette entry, or None when the modal was dismissed.
        """
        if entry is None:
            return
        entry.handler()


def run(project_root: Path | None = None, profile_name: str | None = None) -> None:
    """Construct and run the aim Textual app.

    Args:
        project_root: Directory the TUI operates on; defaults to the cwd.
        profile_name: Layout profile to activate on mount, if any.
    """
    AimApp(project_root=project_root, profile_name=profile_name).run()
