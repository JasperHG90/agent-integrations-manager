"""Agents browser: list/search across all indexed sub-agents, install into projects."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from aim.core import agent_install as install_mod
from aim.core import agents, git, manifest, repos
from aim.tui.modals.agent_install import AgentInstallConfig, AgentInstallModal
from aim.tui.modals.agent_view import AgentViewModal


class AgentsScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("slash", "focus_search", "Search"),
        ("f", "cycle_repo_filter", "Filter by repo"),
        ("enter", "view_current", "View"),
        ("v", "view_current", "View"),
        ("i", "install_current", "Install"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._repo_filter: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Subagents", id="title", markup=False)
        yield Input(placeholder="search…", id="search-bar")
        yield DataTable(id="agents-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[/] Search  [f] Repo filter  [enter/v] View  [i] Install  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("qualified name", "title", "description", "model")
        self._populate("")
        table.focus()

    def on_screen_resume(self) -> None:
        query = self.query_one("#search-bar", Input).value
        self._populate(query)

    def _populate(self, query: str) -> None:
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        rows = agents.search(query) if query else agents.list_agents()
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""
        if not rows:
            if not query and self._repo_filter is None:
                self._status("no subagents indexed — add a repo from the Repos screen")
            else:
                bits = []
                if query:
                    bits.append(f"{query!r}")
                if self._repo_filter:
                    bits.append(f"repo={self._repo_filter}")
                self._status("no matches for " + " ".join(bits))
            return
        for r in rows:
            table.add_row(
                r.qualified_name,
                r.title or "",
                (r.description or "")[:50],
                r.model or "",
                key=r.qualified_name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{len(rows)} subagent(s){filter_label}")

    def action_cycle_repo_filter(self) -> None:
        aliases = [r.alias for r in repos.list_repos()]
        if not aliases:
            self.app.notify("no repos to filter by", severity="warning")
            return
        if self._repo_filter is None:
            self._repo_filter = aliases[0]
        else:
            try:
                idx = aliases.index(self._repo_filter)
            except ValueError:
                idx = -1
            self._repo_filter = aliases[idx + 1] if idx + 1 < len(aliases) else None
        query = self.query_one("#search-bar", Input).value
        self._populate(query)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._populate(event.value)

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_view_current(self) -> None:
        qn = self._selected()
        if qn is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no subagents indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        try:
            content = agents.read_agent_content(qn)
        except agents.AgentNotIndexedError as exc:
            self.app.notify(f"view failed: {exc}", severity="error")
            return
        self.app.push_screen(AgentViewModal(qn, content))

    def action_install_current(self) -> None:
        qn = self._selected()
        if qn is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no subagents indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        self.app.push_screen(
            AgentInstallModal(qn),
            lambda cfg: self._install(qn, cfg),
        )

    def _install(self, qualified_name: str, cfg: AgentInstallConfig | None) -> None:
        if cfg is None:
            return
        try:
            result = install_mod.install(
                cfg.project_root, qualified_name, pin=cfg.pin, track=cfg.track
            )
        except (
            install_mod.AgentNotIndexedError,
            manifest.ManifestNotFoundError,
            git.GitError,
        ) as exc:
            self.app.notify(f"install failed: {exc}", severity="error")
            return
        self.app.notify(
            f"installed {qualified_name} {result.current.identifier()} -> {result.target_path}",
            title="Agent installed",
        )
        for warn in install_mod.take_install_warnings():
            self.app.notify(warn, severity="warning")

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
