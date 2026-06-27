"""Plugins browser: list/search across all indexed plugins, install into projects."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from aim.core import git, install, manifest, plugin_install, plugins, repos, risk
from aim.tui import errors as tui_errors
from aim.tui.modals.plugin_install import PluginInstallConfig, PluginInstallModal
from aim.tui.modals.plugin_view import PluginViewModal
from aim.tui.modals.repo_filter import RepoFilterModal, RepoFilterPick

_PLUGIN_DEPLOY_ERRORS: tuple[type[BaseException], ...] = (  # noqa: RUF005
    plugins.PluginNotIndexedError,
    plugin_install.PluginFlavorUnsupportedError,
    plugin_install.PluginPinError,
    install.ManifestPathEscapeError,
    install.RollbackUnavailableError,
    manifest.ManifestNotFoundError,
    git.GitError,
) + tui_errors.GOVERNANCE_ERRORS

# DataTable row keys must be unique, but a plugin name can repeat across kinds
# (a "claude" and an "opencode" plugin both named "logger"). Encode the identity
# pair into one key string and decode it back when a row is acted on.
_ROW_KEY_SEP = "\t"


def _row_key(qualified_name: str, flavor: str) -> str:
    """Build a unique DataTable row key from a plugin's (qualified_name, flavor)."""
    return f"{qualified_name}{_ROW_KEY_SEP}{flavor}"


def _parse_row_key(key: str) -> tuple[str, str]:
    """Decode a composite row key back into its (qualified_name, flavor) pair."""
    qualified_name, _, flavor = key.partition(_ROW_KEY_SEP)
    return qualified_name, flavor


class PluginsScreen(Screen[None]):
    """Browse, search, view, and install indexed plugins into a project."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("slash", "focus_search", "Search"),
        ("f", "pick_repo_filter", "Filter by repo"),
        ("enter", "view_current", "View"),
        ("v", "view_current", "View"),
        ("i", "install_current", "Install"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialize the screen with no active repo filter or pending install.

        Args:
            project_root: Project the screen operates on; its ``.aim/targets`` specs are
                honored in discovery, view, and install. Falls back to the cwd.
        """
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()
        self._repo_filter: str | None = None
        self._installing: tuple[str, str, PluginInstallConfig] | None = None

    def compose(self) -> ComposeResult:
        """Yield the title, search bar, plugins table, status line, and hint."""
        yield Static("Plugins", id="title", markup=False)
        yield Input(placeholder="search…", id="search-bar")
        yield DataTable(id="plugins-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[/] Search  [f] Repo filter  [enter/v] View  [i] Install  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Set up table columns, populate all plugins, and focus the table."""
        table = self.query_one(DataTable)
        table.add_columns("qualified name", "target", "sha", "description")
        self._populate("")
        table.focus()

    def on_screen_resume(self) -> None:
        """Repopulate the table using the current search query when resumed."""
        query = self.query_one("#search-bar", Input).value
        self._populate(query)

    def _populate(self, query: str) -> None:
        """Refresh the table with plugins matching the query and repo filter.

        Args:
            query: Search string; when empty, all indexed plugins are listed.
        """
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        rows = (
            plugins.search(query, project_root=self._project_root)
            if query
            else plugins.list_plugins(project_root=self._project_root)
        )
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""
        if not rows:
            if not query and self._repo_filter is None:
                self._status("no plugins indexed — add a marketplace repo from the Repos screen")
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
                r.flavor,
                r.short_sha,
                (r.description or "")[:50],
                # Composite key: the same name under different kinds must not collide.
                key=_row_key(r.qualified_name, r.flavor),
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(_row_key(*selected)), animate=False)
            except Exception:
                pass
        self._status(f"{len(rows)} plugin(s){filter_label}")

    def action_pick_repo_filter(self) -> None:
        """Open a picker to filter by a single repo (or clear the filter)."""
        aliases = [r.alias for r in repos.list_repos()]
        if not aliases:
            self.app.notify("no repos to filter by", severity="warning")
            return
        self.app.push_screen(RepoFilterModal(aliases, self._repo_filter), self._on_repo_filter)

    def _on_repo_filter(self, pick: RepoFilterPick | None) -> None:
        """Apply the chosen repo filter, or do nothing when cancelled."""
        if pick is None:
            return
        self._repo_filter = pick.alias
        self._populate(self.query_one("#search-bar", Input).value)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Repopulate the table as the user types in the search bar."""
        if event.input.id == "search-bar":
            self._populate(event.value)

    def action_focus_search(self) -> None:
        """Move keyboard focus to the search bar."""
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> tuple[str, str] | None:
        """Return the (qualified_name, flavor) of the highlighted row, or None if empty."""
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key is None or row_key.value is None:
            return None
        return _parse_row_key(str(row_key.value))

    def action_view_current(self) -> None:
        """Open the selected plugin's full description + manifest in a scrollable modal."""
        selected = self._selected()
        if selected is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no plugins indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        qn, flavor = selected
        try:
            row = plugins.index_row(qn, flavor, self._project_root)
            content = plugins.read_plugin_content(qn, flavor, self._project_root)
        except plugins.PluginNotIndexedError as exc:
            self.app.notify(f"view failed: {exc}", severity="error")
            return
        # The table truncates the description; show it in full here (the modal's
        # text area scrolls), above the raw manifest.
        meta = [f"{row.qualified_name}  [{row.flavor}]"]
        if row.marketplace_name:
            meta.append(f"marketplace: {row.marketplace_name}")
        if row.version:
            meta.append(f"version: {row.version}")
        meta.append(f"sha: {row.short_sha}")
        if row.description:
            meta += ["", row.description]
        body = "\n".join(meta) + "\n\n---\n\n" + content
        self.app.push_screen(PluginViewModal(qn, body))

    def action_install_current(self) -> None:
        """Prompt for install config for the selected plugin, then install it."""
        selected = self._selected()
        if selected is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no plugins indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        qn, flavor = selected
        self.app.push_screen(
            PluginInstallModal(qn, initial_project=self._project_root),
            lambda cfg: self._install(qn, flavor, cfg),
        )

    def _install(self, qualified_name: str, flavor: str, cfg: PluginInstallConfig | None) -> None:
        """Kick off a threaded install for a plugin, or no-op if config is None.

        Args:
            qualified_name: Fully qualified name of the plugin to install.
            flavor: The plugin's kind, disambiguating same-name-different-kind rows.
            cfg: Install configuration from the modal; None means the user cancelled.
        """
        if cfg is None:
            return
        # The risk scan can pull a model or call a judge — run off the UI thread so the
        # TUI doesn't freeze. Errors/warnings are surfaced from the worker.
        self._installing = (qualified_name, flavor, cfg)
        self._status(f"scanning {qualified_name}…")
        self.run_worker(self._do_install_thread, exclusive=True, thread=True)

    def _do_install_thread(self) -> None:
        """Run the pending install on a worker thread and report results to the UI."""
        if self._installing is None:
            return
        qualified_name, flavor, cfg = self._installing
        try:
            result = plugin_install.install_plugin(
                cfg.project_root,
                qualified_name,
                flavor=flavor,
                pin=cfg.pin,
                track=cfg.track,
                override_risk=cfg.override_risk,
            )
        except _PLUGIN_DEPLOY_ERRORS as exc:
            self.app.call_from_thread(self.app.notify, f"install failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"install failed: {exc}")
            return
        self.app.call_from_thread(
            self.app.notify,
            f"installed {qualified_name} {result.current.identifier()} -> {result.target_dir}",
            title="Plugin installed",
        )
        self.app.call_from_thread(self._status, f"installed {qualified_name}")
        # Surface the bundled executable surface (hooks, MCP launchers) and any risk notes.
        for warn in plugin_install.take_install_warnings():
            self.app.call_from_thread(self.app.notify, warn, severity="warning", title="review")
        for warn in risk.take_risk_warnings():
            self.app.call_from_thread(self.app.notify, warn, severity="warning", title="risk")

    def _status(self, msg: str) -> None:
        """Update the status line with the given message."""
        self.query_one("#status", Static).update(msg)
