"""Skills browser: list/search across all indexed skills, install into projects."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from aim.core import git, install, manifest, repos, risk, skills
from aim.tui import errors as tui_errors
from aim.tui.modals.repo_filter import RepoFilterModal, RepoFilterPick
from aim.tui.modals.skill_install import SkillInstallConfig, SkillInstallModal
from aim.tui.modals.skill_view import SkillViewModal

_SKILL_DEPLOY_ERRORS: tuple[type[BaseException], ...] = (  # noqa: RUF005
    install.SkillNotIndexedError,
    install.RollbackUnavailableError,
    manifest.ManifestNotFoundError,
    git.GitError,
) + tui_errors.GOVERNANCE_ERRORS


class SkillsScreen(Screen[None]):
    """Browse, search, view, and install indexed skills into a project."""

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

    def __init__(self) -> None:
        """Initialize the screen with no active repo filter or pending install."""
        super().__init__()
        self._repo_filter: str | None = None
        self._installing: tuple[str, SkillInstallConfig] | None = None

    def compose(self) -> ComposeResult:
        """Yield the title, search bar, skills table, status line, and hint."""
        yield Static("Skills", id="title", markup=False)
        yield Input(placeholder="search…", id="search-bar")
        yield DataTable(id="skills-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[/] Search  [f] Repo filter  [enter/v] View  [i] Install  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Set up table columns, populate all skills, and focus the table."""
        table = self.query_one(DataTable)
        table.add_columns("qualified name", "title", "description")
        self._populate("")
        table.focus()

    def on_screen_resume(self) -> None:
        """Repopulate the table using the current search query when resumed."""
        query = self.query_one("#search-bar", Input).value
        self._populate(query)

    def _populate(self, query: str) -> None:
        """Refresh the table with skills matching the query and repo filter.

        Args:
            query: Search string; when empty, all indexed skills are listed.
        """
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        rows = skills.search(query) if query else skills.list_skills()
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""
        if not rows:
            if not query and self._repo_filter is None:
                self._status("no skills indexed — add a repo from the Repos screen")
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
        self._status(f"{len(rows)} skill(s){filter_label}")

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

    def _selected(self) -> str | None:
        """Return the qualified name of the highlighted row, or None if empty."""
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_view_current(self) -> None:
        """Open the selected skill's content in a read-only view modal."""
        qn = self._selected()
        if qn is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no skills indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        try:
            content = skills.read_skill_content(qn)
        except skills.SkillNotIndexedError as exc:
            self.app.notify(f"view failed: {exc}", severity="error")
            return
        self.app.push_screen(SkillViewModal(qn, content))

    def action_install_current(self) -> None:
        """Prompt for install config for the selected skill, then install it."""
        qn = self._selected()
        if qn is None:
            if self.query_one(DataTable).row_count == 0:
                self.app.notify("no skills indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        self.app.push_screen(
            SkillInstallModal(qn),
            lambda cfg: self._install(qn, cfg),
        )

    def _install(self, qualified_name: str, cfg: SkillInstallConfig | None) -> None:
        """Kick off a threaded install for a skill, or no-op if config is None.

        Args:
            qualified_name: Fully qualified name of the skill to install.
            cfg: Install configuration from the modal; None means the user cancelled.
        """
        if cfg is None:
            return
        # The risk scan can pull a model or call a judge — run off the UI thread so the
        # TUI doesn't freeze. Errors/warnings are surfaced from the worker.
        self._installing = (qualified_name, cfg)
        self._status(f"scanning {qualified_name}…")
        self.run_worker(self._do_install_thread, exclusive=True, thread=True)

    def _do_install_thread(self) -> None:
        """Run the pending install on a worker thread and report results to the UI."""
        if self._installing is None:
            return
        qualified_name, cfg = self._installing
        try:
            result = install.install(cfg.project_root, qualified_name, pin=cfg.pin, track=cfg.track)
        except _SKILL_DEPLOY_ERRORS as exc:
            self.app.call_from_thread(self.app.notify, f"install failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"install failed: {exc}")
            return
        self.app.call_from_thread(
            self.app.notify,
            f"installed {qualified_name} {result.current.identifier()} -> {result.target_dir}",
            title="Skill installed",
        )
        self.app.call_from_thread(self._status, f"installed {qualified_name}")
        for warn in risk.take_risk_warnings():
            self.app.call_from_thread(self.app.notify, warn, severity="warning", title="risk")

    def _status(self, msg: str) -> None:
        """Update the status line with the given message."""
        self.query_one("#status", Static).update(msg)
