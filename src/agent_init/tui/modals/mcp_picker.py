"""Modal: pick an MCP server from the public registry or cached defaults."""

from __future__ import annotations

from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from agent_init.core import default_mcp_servers, mcp_registry


@dataclass(frozen=True)
class McpPick:
    server: mcp_registry.McpServer


class _PickerDataTable(DataTable):
    """DataTable that forwards Enter to the picker's pick action."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.screen.action_pick()
        else:
            super().on_key(event)


class McpPickerModal(ModalScreen[McpPick | None]):
    BINDINGS = [
        Binding("escape", "action_cancel", "Cancel", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
        Binding("enter", "action_pick", "Pick", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[mcp_registry.McpSearchResult] = []
        self._last_query: str = ""

    def compose(self) -> ComposeResult:
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
        table = self.query_one("#mcp-table", DataTable)
        table.add_columns("name", "version", "description", "status")
        self._show_cached()
        self.query_one("#search-bar", Input).focus()

    def _show_cached(self) -> None:
        try:
            defaults = mcp_registry.seed_default_servers(
                default_mcp_servers.DEFAULT_MCP_SERVER_NAMES
            )
        except Exception:
            defaults = {}
        rows: list[mcp_registry.McpSearchResult] = [
            mcp_registry.McpSearchResult(server=server, meta={"isDefault": True})
            for server in defaults.values()
        ]
        for _name, server, _fetched_at, _valid_until in mcp_registry.list_cached_servers():
            if not any(r.server.name == server.name for r in rows):
                rows.append(
                    mcp_registry.McpSearchResult(server=server, meta={"cached": True})
                )
        self._results = rows
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        if not rows:
            self._status("type a search query")
            return
        self._add_rows(rows)
        self._status(f"{len(rows)} cached/default server(s)")

    def _add_rows(self, results: list[mcp_registry.McpSearchResult]) -> None:
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
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        q = query.strip()
        if not q:
            self._show_cached()
            return
        self._last_query = q
        self._status(f"searching for {q!r}…")
        self.run_worker(
            lambda: self._search_worker(q),
            name="mcp_picker_search",
            group="mcp_picker_search",
            thread=True,
        )

    def _search_worker(self, q: str) -> None:
        try:
            results, next_cursor = mcp_registry.search_registry(q)
        except mcp_registry.McpRegistryError as exc:
            self.app.call_from_thread(self._on_search_error, str(exc))
            return
        self.app.call_from_thread(self._on_search_results, results, next_cursor)

    def _on_search_results(
        self,
        results: list[mcp_registry.McpSearchResult],
        next_cursor: str | None,
    ) -> None:
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
        self.app.notify(f"registry search failed: {message}", severity="error")
        self._status("registry search failed")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-bar":
            self._populate(event.value)
            self.query_one("#mcp-table", DataTable).focus()
            self.action_pick()
            return

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def _selected_name(self) -> str | None:
        table = self.query_one("#mcp-table", DataTable)
        if table.row_count == 0 or not self._results:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _selected(self) -> mcp_registry.McpServer | None:
        name = self._selected_name()
        if name is None:
            return None
        for r in self._results:
            if r.server.name == name:
                return r.server
        return None

    def action_pick(self) -> None:
        server = self._selected()
        if server is None:
            self._status("no MCP server selected")
            return
        self.dismiss(McpPick(server=server))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self.action_pick()
        else:
            self.action_cancel()

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
