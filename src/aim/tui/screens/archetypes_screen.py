"""Archetypes browser: list/search instruction archetypes across repos, view a
base body, and select one as the current project's AGENTS.md base."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from aim.core import archetype_install, archetypes, git, risk
from aim.tui import errors as tui_errors
from aim.tui.modals.repo_filter import RepoFilterModal, RepoFilterPick
from aim.tui.modals.skill_view import SkillViewModal

_ARCHETYPE_DEPLOY_ERRORS: tuple[type[BaseException], ...] = (  # noqa: RUF005
    archetypes.ArchetypeNotIndexedError,
    git.GitError,
) + tui_errors.GOVERNANCE_ERRORS

# Sentinel row for aim's bundled AGENTS.md scaffold (no repo archetype). Selecting
# it reverts the project to the built-in template.
_BUILTIN = "default"


class ArchetypesScreen(Screen[None]):
    """Browse, search, view, and select project-instruction archetypes."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("slash", "focus_search", "Search"),
        ("f", "pick_repo_filter", "Filter by repo"),
        ("enter", "view_current", "View"),
        ("v", "view_current", "View"),
        ("u", "use_current", "Use as base"),
        ("c", "clear_archetype", "Clear base"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialize the screen for a project with no active filter."""
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()
        self._repo_filter: str | None = None
        self._using: str | None = None

    def compose(self) -> ComposeResult:
        """Yield the title, search bar, table, status line, and hint."""
        yield Static("Archetypes", id="title", markup=False)
        yield Input(placeholder="search…", id="search-bar")
        yield DataTable(id="archetypes-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[/] Search  [f] Repo filter  [enter/v] View  [u] Use as base  "
            "[c] Clear  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Set up columns, populate all archetypes, and focus the table."""
        table = self.query_one(DataTable)
        table.add_columns("qualified name", "title", "files", "description")
        self._populate("")
        table.focus()

    def on_screen_resume(self) -> None:
        """Repopulate using the current search query when resumed."""
        self._populate(self.query_one("#search-bar", Input).value)

    def _populate(self, query: str) -> None:
        """Refresh the table with archetypes matching the query and repo filter."""
        table = self.query_one(DataTable)
        selected = self._selected()
        table.clear()
        rows = archetypes.search(query) if query else archetypes.list_archetypes()
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""

        # The built-in default always heads the browse list (it ships with aim);
        # selecting it reverts to the bundled scaffold. It is scoped out when a
        # search or repo filter is active.
        show_builtin = not query and self._repo_filter is None
        if show_builtin:
            table.add_row(
                _BUILTIN,
                "Built-in template",
                "-",
                "aim's bundled AGENTS.md scaffold (no archetype)",
                key=_BUILTIN,
            )
        for r in rows:
            table.add_row(
                r.qualified_name,
                r.title or "",
                r.available or "",
                (r.description or "")[:50],
                key=r.qualified_name,
            )
        total = (1 if show_builtin else 0) + len(rows)
        if total == 0:
            self._status("no matches" if query else "no archetypes indexed — add a repo")
            return
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{total} archetype(s){filter_label}")

    def action_pick_repo_filter(self) -> None:
        """Open a picker to filter by a single repo (or clear the filter)."""
        from aim.core import repos

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
        """Repopulate as the user types in the search bar."""
        if event.input.id == "search-bar":
            self._populate(event.value)

    def action_focus_search(self) -> None:
        """Move focus to the search bar."""
        self.query_one("#search-bar", Input).focus()

    def _selected(self) -> str | None:
        """Return the qualified name of the highlighted row, or None if empty."""
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_view_current(self) -> None:
        """Open the selected archetype's base instruction body in a read-only modal."""
        qn = self._selected()
        if qn is None:
            self._status("no row selected")
            return
        if qn == _BUILTIN:
            self.app.notify("the built-in template has no repo body to view", severity="warning")
            return
        try:
            row = archetypes.index_row(qn)
            body = archetypes.read_base_body(
                row.repo_alias, row.indexed_at_sha, row.instruction_path
            )
        except (archetypes.ArchetypeNotIndexedError, git.GitError) as exc:
            self.app.notify(f"view failed: {exc}", severity="error")
            return
        self.app.push_screen(SkillViewModal(qn, body))

    def action_use_current(self) -> None:
        """Select the highlighted archetype as the project's AGENTS.md base."""
        qn = self._selected()
        if qn is None:
            self._status("no row selected")
            return
        self._using = qn
        self._status(f"selecting {qn}…")
        self.run_worker(self._do_use_thread, exclusive=True, thread=True)

    def _do_use_thread(self) -> None:
        """Run the archetype selection on a worker thread and report results."""
        if self._using is None:
            return
        qn = self._using
        try:
            if qn == _BUILTIN:
                archetype_install.clear(self._project_root)
            else:
                archetype_install.select(self._project_root, qn)
        except _ARCHETYPE_DEPLOY_ERRORS as exc:
            self.app.call_from_thread(self.app.notify, f"select failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"select failed: {exc}")
            return
        base = "the built-in template" if qn == _BUILTIN else qn
        self.app.call_from_thread(
            self.app.notify,
            f"{base} is now the AGENTS.md base — run Lock then Sync to render it",
            title="Archetype selected",
        )
        self.app.call_from_thread(self._status, f"selected {base}")
        for warn in risk.take_risk_warnings():
            self.app.call_from_thread(self.app.notify, warn, severity="warning", title="risk")

    def action_clear_archetype(self) -> None:
        """Clear the selected archetype, reverting AGENTS.md to the built-in template."""
        try:
            archetype_install.clear(self._project_root)
        except git.GitError as exc:
            self.app.notify(f"clear failed: {exc}", severity="error")
            return
        self.app.notify("archetype cleared — run Lock then Sync", title="Archetype cleared")
        self._status("archetype cleared")

    def _status(self, msg: str) -> None:
        """Update the status line with the given message."""
        self.query_one("#status", Static).update(msg)
