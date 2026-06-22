"""Rules browser: list/search across all indexed rules, add into projects."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from aim.core import git, manifest, repo_rules, repos, risk, rule_install
from aim.tui import errors as tui_errors
from aim.tui.modals.repo_filter import RepoFilterModal, RepoFilterPick
from aim.tui.modals.rule_install import RuleInstallConfig, RuleInstallModal
from aim.tui.modals.rule_view import RuleViewModal

_RULE_DEPLOY_ERRORS: tuple[type[BaseException], ...] = (  # noqa: RUF005
    rule_install.RuleNotIndexedError,
    manifest.ManifestNotFoundError,
    git.GitError,
) + tui_errors.GOVERNANCE_ERRORS


class RulesScreen(Screen[None]):
    """Browse, search, filter, and install indexed rules into a project."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("slash", "focus_search", "Search"),
        ("f", "pick_repo_filter", "Filter by repo"),
        ("enter", "view_current", "View"),
        ("v", "view_current", "View"),
        ("i", "install_current", "Add"),
        ("a", "install_current", "Add"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        """Initialize the screen with no repo filter and no install in flight."""
        super().__init__()
        self._repo_filter: str | None = None
        self._installing: tuple[str, RuleInstallConfig] | None = None

    def compose(self) -> ComposeResult:
        """Build the title, search bar, rules table, status line, and hint."""
        yield Static("Rules", id="title", markup=False)
        yield Input(placeholder="search…", id="search-bar")
        yield DataTable(id="rules-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[/] Search  [f] Repo filter  [enter/v] View  [a/i] Add  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Set up the table columns, populate all rules, and focus the table."""
        table = self.query_one(DataTable)
        table.add_columns("qualified name", "title", "description")
        self._populate("")
        table.focus()

    def on_screen_resume(self) -> None:
        """Repopulate the table using the current search query when resumed."""
        query = self.query_one("#search-bar", Input).value
        self._populate(query)

    def _populate(self, query: str) -> None:
        """Refill the rules table from a search query and the active repo filter.

        Args:
            query: Search text; when empty, all indexed rules are listed.
        """
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        rows = repo_rules.search(query) if query else repo_rules.list_rules()
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""
        if not rows:
            if not query and self._repo_filter is None:
                self._status("no rules indexed — add a repo from the Repos screen")
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
                (r.description or "")[:60],
                key=r.qualified_name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{len(rows)} rule(s){filter_label}")

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
        """Repopulate the table live as the search bar text changes."""
        if event.input.id == "search-bar":
            self._populate(event.value)

    def action_focus_search(self) -> None:
        """Move keyboard focus to the search bar."""
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> str | None:
        """Return the qualified name of the highlighted row, or None if empty.

        Returns:
            The selected rule's qualified name, or None when no row is current.
        """
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_view_current(self) -> None:
        """Open a modal showing the source of the selected rule."""
        qn = self._selected()
        if qn is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no rules indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        try:
            content = repo_rules.read_rule_content(qn)
        except repo_rules.RuleNotIndexedError as exc:
            self.app.notify(f"view failed: {exc}", severity="error")
            return
        self.app.push_screen(RuleViewModal(qn, content))

    def action_install_current(self) -> None:
        """Open the install modal for the selected rule, or warn if none is."""
        qn = self._selected()
        if qn is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no rules indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        self.app.push_screen(
            RuleInstallModal(qn),
            lambda cfg: self._install(qn, cfg),
        )

    def _install(self, qualified_name: str, cfg: RuleInstallConfig | None) -> None:
        """Kick off a background install worker for the given rule and config.

        Args:
            qualified_name: The rule to install.
            cfg: Modal-supplied install options, or None if the user cancelled.
        """
        if cfg is None:
            return
        # Run off the UI thread: the risk scan can pull a model or call a judge.
        self._installing = (qualified_name, cfg)
        self._status(f"scanning {qualified_name}…")
        self.run_worker(self._do_install_thread, exclusive=True, thread=True)

    def _do_install_thread(self) -> None:
        """Run the rule install on a worker thread and report results to the UI.

        Marshals all notifications and status updates back to the app thread and
        surfaces any risk warnings emitted during the install.
        """
        if self._installing is None:
            return
        qualified_name, cfg = self._installing
        try:
            result = rule_install.install(
                cfg.project_root, qualified_name, pin=cfg.pin, track=cfg.track
            )
        except _RULE_DEPLOY_ERRORS as exc:
            self.app.call_from_thread(self.app.notify, f"add failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"add failed: {exc}")
            return
        self.app.call_from_thread(
            self.app.notify,
            f"added {qualified_name} {result.current.identifier()}",
            title="Rule added",
        )
        self.app.call_from_thread(self._status, f"added {qualified_name}")
        for warn in risk.take_risk_warnings():
            self.app.call_from_thread(self.app.notify, warn, severity="warning", title="risk")

    def _status(self, msg: str) -> None:
        """Update the status line with the given message."""
        self.query_one("#status", Static).update(msg)
