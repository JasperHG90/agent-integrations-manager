"""Modal: pick an MCP server from the public registry or cached defaults."""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from aim.core import default_mcp_servers, mcp_registry
from aim.tui import _threads


@dataclass(frozen=True)
class McpPick:
    """Result returned when the user picks an MCP server from the modal."""

    server: mcp_registry.McpServer


class _PickerDataTable(DataTable):
    """DataTable that forwards Enter to the picker's pick action."""

    def on_key(self, event: events.Key) -> None:
        """Trigger the picker's pick action on Enter, otherwise defer to default."""
        if event.key == "enter":
            event.stop()
            screen = self.screen
            assert isinstance(screen, McpPickerModal)
            screen.action_pick()
        else:
            super().on_key(event)  # type: ignore[misc]


class McpPickerModal(ModalScreen[McpPick | None]):
    """Modal screen for searching and selecting an MCP server to add."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("enter", "pick", "Pick", priority=True),
    ]

    def __init__(self) -> None:
        """Initialize the modal with empty results and no prior query."""
        super().__init__()
        self._results: list[mcp_registry.McpSearchResult] = []
        self._last_query: str = ""
        self._search_seq = 0

    def compose(self) -> ComposeResult:
        """Build the modal layout with search bar, results table, and buttons."""
        yield Vertical(
            Static("Add MCP server", classes="modal-title", markup=False),
            Input(placeholder="search registry…", id="search-bar"),
            _PickerDataTable(id="mcp-table", cursor_type="row"),
            Static("", id="status", markup=False),
            Horizontal(
                Button("Add", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Set up the table columns, show cached servers, and focus the search bar."""
        table = self.query_one("#mcp-table", DataTable)
        table.add_columns("name", "version", "description", "status")
        self._show_cached()
        self.query_one("#search-bar", Input).focus()

    def _show_cached(self) -> None:
        """Populate the table with default and cached servers from the registry."""
        try:
            defaults = mcp_registry.seed_default_servers(
                default_mcp_servers.DEFAULT_MCP_SERVER_NAMES
            )
        except Exception:
            defaults = {}
        rows: list[mcp_registry.McpSearchResult] = [
            mcp_registry.McpSearchResult(server=server, _meta={"isDefault": True})
            for server in defaults.values()
        ]
        for _name, server, _fetched_at, _valid_until in mcp_registry.list_cached_servers():
            if not any(r.server.name == server.name for r in rows):
                rows.append(mcp_registry.McpSearchResult(server=server, _meta={"cached": True}))
        self._results = rows
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        if not rows:
            self._status("type a search query")
            return
        self._add_rows(rows)
        self._status(f"{len(rows)} cached/default server(s)")

    def _add_rows(self, results: list[mcp_registry.McpSearchResult]) -> None:
        """Append search results to the table, deduplicating by server name.

        Args:
            results: Search results to render as rows; duplicates by name are skipped.
        """
        table = self.query_one("#mcp-table", DataTable)
        seen: set[str] = set()
        for r in results:
            s = r.server
            if s.name in seen:
                continue
            seen.add(s.name)
            meta = r.meta
            if meta.get("isDefault"):
                status = "default"
            elif meta.get("cached"):
                status = "cached"
            else:
                status = ""
            table.add_row(
                s.name,
                s.version or "?",
                (s.description or "")[:60],
                status,
                key=s.name,
            )

    def _populate(self, query: str) -> None:
        """Refresh the table for a query, showing cached servers when query is empty.

        Args:
            query: Raw search text; an empty/whitespace query falls back to cached servers.
        """
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        q = query.strip()
        if not q:
            self._show_cached()
            return
        self._last_query = q
        self._status(f"searching for {q!r}…")
        # Daemon thread, not a Textual worker: worker threads are joined at
        # interpreter exit, so a search mid-flight stalled quitting (see
        # aim.tui._threads). Staleness is handled by the sequence guard.
        self._search_seq += 1
        seq = self._search_seq
        _threads.run_detached(lambda: self._search_worker(q, seq), name="mcp-picker-search")

    def _search_worker(self, q: str, seq: int) -> None:
        """Search the registry off-thread and marshal results back to the UI thread.

        Args:
            q: Query string to search the registry for.
            seq: Search sequence captured at launch; a stale thread (the user
                typed again) delivers nothing.
        """
        try:
            results, next_cursor = mcp_registry.search_registry(q)
        except mcp_registry.McpRegistryError as exc:
            self._deliver(seq, self._on_search_error, str(exc))
            return
        self._deliver(seq, self._on_search_results, results, next_cursor)

    def _deliver(self, seq: int, callback, *args) -> None:  # type: ignore[no-untyped-def]
        """Marshal a search outcome to the UI thread unless it went stale."""

        def _apply() -> None:
            if seq == self._search_seq:
                callback(*args)

        try:
            self.app.call_from_thread(_apply)
        except Exception:
            pass  # app shut down or modal dismissed mid-search

    def _on_search_results(
        self,
        results: list[mcp_registry.McpSearchResult],
        next_cursor: str | None,
    ) -> None:
        """Render search results, preserving the cursor selection when possible.

        Args:
            results: Servers returned by the registry search.
            next_cursor: Pagination cursor; when set, more results are available.
        """
        table = self.query_one("#mcp-table", DataTable)
        selected = self._selected_name()
        self._results = results
        table.clear()
        if not results:
            self._status(f"no MCP servers match {self._last_query!r}")
            return
        self._add_rows(results)
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        tail = " (more available)" if next_cursor else ""
        self._status(f"{len(results)} result(s){tail}")

    def _on_search_error(self, message: str) -> None:
        """Notify the user and update status when a registry search fails.

        Args:
            message: Human-readable error detail from the failed search.
        """
        self.app.notify(f"registry search failed: {message}", severity="error")
        self._status("registry search failed")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the search and immediately pick the top result on Enter in the search bar."""
        if event.input.id == "search-bar":
            self._populate(event.value)
            self.query_one("#mcp-table", DataTable).focus()
            self.action_pick()
            return

    def action_focus_search(self) -> None:
        """Move keyboard focus to the search input."""
        self.query_one("#search-bar", Input).focus()

    def _selected_name(self) -> str | None:
        """Return the name of the currently highlighted server, or None.

        Returns:
            The selected row's server name, or None when nothing is selected.
        """
        table = self.query_one("#mcp-table", DataTable)
        if table.row_count == 0 or not self._results:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _selected(self) -> mcp_registry.McpServer | None:
        """Return the currently highlighted server object, or None.

        Returns:
            The selected McpServer, or None when nothing is selected.
        """
        name = self._selected_name()
        if name is None:
            return None
        for r in self._results:
            if r.server.name == name:
                return r.server
        return None

    def action_pick(self) -> None:
        """Dismiss the modal with the selected server, or report none was chosen."""
        server = self._selected()
        if server is None:
            self._status("no MCP server selected")
            return
        self.dismiss(McpPick(server=server))

    def action_cancel(self) -> None:
        """Dismiss the modal without selecting a server."""
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Pick on the Add button, otherwise cancel the modal."""
        if event.button.id == "go":
            self.action_pick()
        else:
            self.action_cancel()

    def _status(self, msg: str) -> None:
        """Update the modal's status line text.

        Args:
            msg: Message to display in the status area.
        """
        self.query_one("#status", Static).update(msg)
