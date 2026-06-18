"""MCP registry browser: search public registry, install into projects."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static
from textual.worker import WorkerState, get_current_worker

from aim.core import default_mcp_servers, manifest, mcp_registry, validation
from aim.core import mcp_install as install_mod
from aim.tui.modals.mcp_install import McpInstallConfig, McpInstallModal


class McpScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("slash", "focus_search", "Search"),
        ("enter", "enter", "View / Search"),
        ("v", "enter", "View"),
        ("i", "install_current", "Install"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()
        self._results: list[mcp_registry.McpSearchResult] = []
        self._last_query: str = ""
        self._default_results: list[mcp_registry.McpSearchResult] | None = None
        self._cached_results: list[mcp_registry.McpSearchResult] | None = None
        self._installed_results: list[mcp_registry.McpSearchResult] | None = None
        self._installing: tuple[mcp_registry.McpServer, McpInstallConfig] | None = None

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
        table.add_columns("name", "version", "description", "status")
        self._installed_results = self._load_installed()
        if self._default_results is None:
            self._status("loading default MCP servers…")
            self.run_worker(self._load_defaults, group="mcp_defaults", thread=True)
        else:
            self._populate("")
            table.focus()

    def _load_installed(self) -> list[mcp_registry.McpSearchResult]:
        try:
            m = manifest.load_or_default(self._project_root)
        except Exception:
            return []
        out: list[mcp_registry.McpSearchResult] = []
        for installed in m.mcp_servers:
            server = mcp_registry.McpServer(
                name=installed.registry_name,
                description=None,
                title=None,
                version=installed.current.registry_version,
            )
            out.append(
                mcp_registry.McpSearchResult(
                    server=server,
                    _meta={"installed": True, "alias": installed.alias},
                )
            )
        return out

    def _load_defaults(self) -> None:
        worker = get_current_worker()
        if worker.is_cancelled:
            return
        try:
            servers = mcp_registry.seed_default_servers(
                default_mcp_servers.DEFAULT_MCP_SERVER_NAMES
            )
        except Exception:
            servers = {}
        defaults = [
            mcp_registry.McpSearchResult(server=server, _meta={"isDefault": True})
            for server in servers.values()
        ]
        cached = self._load_cached_servers()
        self.app.call_from_thread(self._on_defaults_loaded, defaults, cached)

    def _load_cached_servers(self) -> list[mcp_registry.McpSearchResult]:
        worker = get_current_worker()
        if worker.is_cancelled:
            return []
        out: list[mcp_registry.McpSearchResult] = []
        for _name, server, fetched_at, valid_until in mcp_registry.list_cached_servers():
            out.append(
                mcp_registry.McpSearchResult(
                    server=server,
                    _meta={
                        "cached": True,
                        "fetched_at": fetched_at.isoformat(),
                        "valid_until": valid_until.isoformat(),
                    },
                )
            )
        return out

    def _on_defaults_loaded(
        self,
        defaults: list[mcp_registry.McpSearchResult],
        cached: list[mcp_registry.McpSearchResult],
    ) -> None:
        self._default_results = defaults
        self._cached_results = cached
        self._populate("")
        self.query_one("#mcp-table", DataTable).focus()

    def _populate(self, query: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._results = []
        q = query.strip()
        if not q:
            self._show_cached()
            return
        self._last_query = q
        self._status(f"searching for {q!r}…")
        self.run_worker(
            lambda: self._search_worker(q),
            name="mcp_search",
            group="mcp_search",
            thread=True,
        )

    def _search_worker(self, q: str) -> None:
        worker = get_current_worker()
        if worker.is_cancelled:
            return
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
        table = self.query_one(DataTable)
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

    def _show_cached(self) -> None:
        installed = self._installed_results or []
        installed_names = {i.server.name for i in installed}
        cached = self._cached_results or []
        defaults = self._default_results or []

        # Start with installed entries, then cached (excluding installed),
        # then defaults (excluding already shown).
        combined = list(installed)
        shown = installed_names.copy()
        for entry in cached + defaults:
            if entry.server.name in shown:
                continue
            combined.append(entry)
            shown.add(entry.server.name)

        table = self.query_one(DataTable)
        selected = self._selected_name()
        self._results = combined
        if not combined:
            self._status("type a search query")
            return
        self._add_rows(combined)
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(
            f"{len(installed)} installed · {len(combined) - len(installed)} cached/default"
        )

    def _selected_name(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or not self._results:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _add_rows(self, results: list[mcp_registry.McpSearchResult]) -> None:
        table = self.query_one(DataTable)
        seen: set[str] = set()
        for r in results:
            s = r.server
            if s.name in seen:
                continue
            seen.add(s.name)
            meta = r.meta
            if meta.get("installed"):
                status = f"installed ({meta.get('alias', '')})"
            elif meta.get("cached"):
                valid_until = meta.get("valid_until", "")
                if valid_until:
                    from datetime import datetime

                    try:
                        until_dt = datetime.fromisoformat(valid_until)
                        status = f"cached (until {until_dt.strftime('%Y-%m-%d %H:%M')})"
                    except Exception:
                        status = "cached"
                else:
                    status = "cached"
            elif meta.get("isDefault"):
                status = "default"
            else:
                status = ""
            table.add_row(
                s.name,
                s.version or "?",
                (s.description or "")[:60],
                status,
                key=s.name,
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-bar":
            self._populate(event.value)
            self.query_one("#mcp-table", DataTable).focus()
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
        self._status(f"installing {server.name} as {cfg.alias}…")
        self._installing = (server, cfg)
        self.run_worker(self._do_install_thread, exclusive=True, thread=True)

    def _do_install_thread(self) -> None:
        installing = getattr(self, "_installing", None)
        if installing is None:
            return
        server, cfg = installing
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
            self.app.call_from_thread(self.app.notify, f"install failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"install failed: {exc}")
            return
        self.app.call_from_thread(
            self.app.notify,
            f"installed MCP server {server.name} as {cfg.alias}",
            title="MCP server installed",
        )
        self.app.call_from_thread(self._status, f"installed {server.name} as {cfg.alias}")

    def on_worker_state_changed(self, event) -> None:  # type: ignore[no-untyped-def]
        installing = getattr(self, "_installing", None)
        if installing is not None:
            if event.state == WorkerState.RUNNING:
                server, cfg = installing
                self._status(f"installing {server.name} as {cfg.alias}…")
            elif event.state in (WorkerState.SUCCESS, WorkerState.CANCELLED, WorkerState.ERROR):
                self._installing = None

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
