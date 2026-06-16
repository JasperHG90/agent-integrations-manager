"""MCP registry browser: search public registry, install into projects."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from agent_init.core import default_mcp_servers, mcp_registry, validation
from agent_init.core import mcp_install as install_mod
from agent_init.tui.modals.mcp_install import McpInstallConfig, McpInstallModal


class McpScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("slash", "focus_search", "Search"),
        ("enter", "action_enter", "View / Search"),
        ("v", "action_enter", "View"),
        ("i", "action_install_current", "Install"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[mcp_registry.McpSearchResult] = []
        self._last_query: str = ""
        self._default_results: list[mcp_registry.McpSearchResult] | None = None

    def compose(self) -> ComposeResult:
        yield Static("MCP servers", id="title", markup=False)
        yield Input(placeholder="search registry…", id="search-bar")
        yield DataTable(id="mcp-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[/] focus search  [enter] search / view  [i] install  [b] back  [q] quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("name", "version", "description")
        self._load_defaults()
        self._populate("")
        table.focus()

    def _load_defaults(self) -> None:
        if self._default_results is not None:
            return
        defaults: list[mcp_registry.McpSearchResult] = []
        for name in default_mcp_servers.DEFAULT_MCP_SERVER_NAMES:
            try:
                server = mcp_registry.find_server(name, exact_name=name)
            except mcp_registry.McpRegistryError:
                continue
            defaults.append(
                mcp_registry.McpSearchResult(server=server, meta={"isDefault": True})
            )
        self._default_results = defaults

    def _populate(self, query: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._results = []
        q = query.strip()
        if not q:
            self._show_defaults()
            return
        try:
            results, next_cursor = mcp_registry.search_registry(q)
        except mcp_registry.McpRegistryError as exc:
            self.app.notify(f"registry search failed: {exc}", severity="error")
            self._status("registry search failed")
            return
        self._results = results
        if not results:
            self._status(f"no MCP servers match {q!r}")
            return
        self._add_rows(results)
        tail = " (more available)" if next_cursor else ""
        self._status(f"{len(results)} result(s){tail}")

    def _show_defaults(self) -> None:
        defaults = self._default_results or []
        self._results = defaults
        if not defaults:
            self._status("type a search query")
            return
        self._add_rows(defaults)
        self._status(f"{len(defaults)} default MCP server(s) (cached)")

    def _add_rows(self, results: list[mcp_registry.McpSearchResult]) -> None:
        table = self.query_one(DataTable)
        seen: set[str] = set()
        for r in results:
            s = r.server
            if s.name in seen:
                continue
            seen.add(s.name)
            table.add_row(
                s.name,
                s.version or "?",
                (s.description or "")[:60],
                key=s.name,
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-bar":
            self._populate(event.value)
            return

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> mcp_registry.McpSearchResult | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or not self._results:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        name = str(row_key.value) if row_key and row_key.value is not None else None
        if name is None:
            return None
        for r in self._results:
            if r.server.name == name:
                return r
        return None

    def action_enter(self) -> None:
        # If the search bar is focused, submitting the Input already ran the
        # search. Otherwise view the currently selected row.
        focused = self.app.focused
        if isinstance(focused, Input) and focused.id == "search-bar":
            return
        self._do_view()

    def _do_view(self) -> None:
        r = self._selected()
        if r is None:
            self._status("no row selected")
            return
        self.app.push_screen(
            McpInstallModal(r.server, editable=False),
            lambda _: None,
        )

    def action_install_current(self) -> None:
        r = self._selected()
        if r is None:
            self._status("no row selected")
            return
        self.app.push_screen(
            McpInstallModal(r.server, editable=True),
            lambda cfg: self._install(r.server, cfg),
        )

    def _install(self, server: mcp_registry.McpServer, cfg: McpInstallConfig | None) -> None:
        if cfg is None:
            return
        if not validation.is_valid_alias(cfg.alias):
            self.app.notify(
                f"alias {cfg.alias!r} invalid: lowercase alphanumeric, _, or -",
                severity="error",
            )
            return
        try:
            install_mod.install(
                cfg.project_root,
                server.name,
                alias=cfg.alias,
                preferred_transport=cfg.transport,
                overrides=cfg.overrides,
                force=cfg.force,
            )
        except (
            install_mod.McpAliasInvalidError,
            install_mod.McpAliasConflictError,
            install_mod.McpOverrideError,
            install_mod.McpLocalEditsError,
            mcp_registry.McpMappingError,
            mcp_registry.McpRegistryError,
        ) as exc:
            self.app.notify(f"install failed: {exc}", severity="error")
            return
        self.app.notify(
            f"installed MCP server {server.name} as {cfg.alias}",
            title="MCP server installed",
        )

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
