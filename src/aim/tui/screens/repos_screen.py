"""Registered-repos screen: list + add + refresh + remove."""

from __future__ import annotations

from datetime import UTC, datetime

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static
from textual.worker import WorkerState

from aim.core import git, repos
from aim.tui.modals.confirm import ConfirmModal
from aim.tui.modals.repo_add import RepoAddModal, RepoAddResult


def kind_tag(kinds: set[str]) -> str:
    """Render a set of artifact kinds as a human-readable tag string.

    Args:
        kinds: Artifact kinds present in a repo (e.g. "skill", "agent", "rules").

    Returns:
        A " + "-joined label, or "—" when no known kinds are present.
    """
    parts: list[str] = []
    if "skill" in kinds:
        parts.append("skills")
    if "agent" in kinds:
        parts.append("subagents")
    if "rules" in kinds:
        parts.append("rules")
    if "archetype" in kinds:
        parts.append("archetypes")
    if "plugin" in kinds:
        parts.append("plugins")
    return " + ".join(parts) if parts else "—"


class ReposScreen(Screen[None]):
    """Screen that lists registered repos and supports add, refresh, and remove."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("a", "add_repo", "Add"),
        ("r", "refresh_current", "Refresh"),
        ("x", "remove_current", "Remove"),
        ("q", "app.quit", "Quit"),
    ]

    _adding: RepoAddResult | None = None
    _refreshing: str | None = None

    def compose(self) -> ComposeResult:
        """Build the title, repos table, status line, and key hint."""
        yield Static("Registered Repos", id="title", markup=False)
        yield DataTable(id="repos-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[a] Add  [r] Refresh  [x] Remove  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Set up the table columns, populate rows, and focus the table."""
        table = self.query_one(DataTable)
        table.add_columns("alias", "url", "head", "last fetched", "contains")
        self._populate()
        table.focus()

    def on_screen_resume(self) -> None:
        """Refresh the table whenever the screen is resumed."""
        self._populate()

    def _populate(self) -> None:
        """Rebuild the table from the current repo registry, preserving selection."""
        table = self.query_one(DataTable)
        selected_alias = self._selected_alias()
        table.clear()
        rows = repos.list_repos()
        if not rows:
            self._status("no repos registered — press [a] to add one")
            return
        now = datetime.now(UTC)
        for r in rows:
            sha = (r.last_sha or "?")[:12]
            when = "?"
            if r.last_fetched_at:
                fetched = r.last_fetched_at
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=UTC)
                when = _humanize((now - fetched).total_seconds())
            tag = kind_tag(repos.artifact_kinds(r.alias))
            table.add_row(r.alias, r.url, sha, when, tag, key=r.alias)
        if selected_alias is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected_alias), animate=False)
            except Exception:
                pass
        self._status(f"{len(rows)} repo(s)")

    def _selected_alias(self) -> str | None:
        """Return the alias under the cursor, or None when the table is empty."""
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_add_repo(self) -> None:
        """Open the add-repo modal and handle its result."""
        self.app.push_screen(RepoAddModal(), self._on_add)

    def _on_add(self, result: RepoAddResult | None) -> None:
        """Kick off a background add for the modal result.

        Args:
            result: The completed add form, or None if the modal was cancelled.
        """
        if result is None:
            return
        self._status(f"adding {result.alias}…")
        self._adding = result
        self.run_worker(self._do_add_thread, exclusive=True, thread=True)

    def _do_add_thread(self) -> None:
        """Add the pending repo on a worker thread, reporting status to the UI."""
        result = self._adding
        if result is None:
            return
        try:
            repos.add(
                result.alias,
                result.url,
                default_ref=result.default_ref,
                allow_empty=result.allow_empty,
            )
        except (
            repos.RepoAliasError,
            repos.RepoExistsError,
            repos.RepoHasNoArtifactsError,
            git.GitError,
        ) as exc:
            self.app.call_from_thread(self.app.notify, f"add failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"add failed: {exc}")
            return
        self.app.call_from_thread(self.app.notify, f"added {result.alias}", title="Repo added")
        self.app.call_from_thread(self._status, f"added {result.alias}")

    def on_worker_state_changed(self, event) -> None:  # type: ignore[no-untyped-def]
        """Update status and repopulate when an add or refresh worker changes state."""
        adding = getattr(self, "_adding", None)
        if adding is not None:
            if event.state == WorkerState.RUNNING:
                self._status(f"adding {adding.alias}…")
            elif event.state == WorkerState.SUCCESS:
                self._adding = None
                self._populate()
            elif event.state in (WorkerState.CANCELLED, WorkerState.ERROR):
                self._adding = None
        refreshing = getattr(self, "_refreshing", None)
        if refreshing is not None:
            if event.state == WorkerState.RUNNING:
                self._status(f"refreshing {refreshing}…")
            elif event.state == WorkerState.SUCCESS:
                self._refreshing = None
                self._populate()
            elif event.state in (WorkerState.CANCELLED, WorkerState.ERROR):
                self._refreshing = None

    def action_refresh_current(self) -> None:
        """Start a background refresh of the selected repo."""
        alias = self._selected_alias()
        if alias is None:
            self._notify_or_status("no row selected")
            return
        self._status(f"refreshing {alias}…")
        self._refreshing = alias
        self.run_worker(self._do_refresh_thread, exclusive=True, thread=True)

    def _do_refresh_thread(self) -> None:
        """Refresh the pending repo on a worker thread, reporting status to the UI."""
        alias = getattr(self, "_refreshing", None)
        if alias is None:
            return
        try:
            repo = repos.refresh(alias)
        except (repos.RepoNotFoundError, repos.RefDisappearedError, git.GitError) as exc:
            self.app.call_from_thread(self.app.notify, f"refresh failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"refresh failed: {exc}")
            return
        self.app.call_from_thread(self.app.notify, f"refreshed {alias}", title="Repo refreshed")
        self.app.call_from_thread(
            self._status, f"refreshed {alias}: HEAD={(repo.last_sha or '?')[:12]}"
        )
        self.app.call_from_thread(self._populate)

    def action_remove_current(self) -> None:
        """Confirm and remove the selected repo, wiping its cache and project artifacts."""
        alias = self._selected_alias()
        if alias is None:
            self._notify_or_status("no row selected")
            return

        def _on_confirm(yes: bool | None) -> None:
            """Perform the removal once the confirm modal returns affirmatively.

            Args:
                yes: The modal result; removal proceeds only when this is True.
            """
            if yes is not True:
                return
            project_root = getattr(self.app, "_project_root", None)
            try:
                repos.remove(alias)
            except repos.RepoNotFoundError as exc:
                self.app.notify(f"remove failed: {exc}", severity="error")
                return
            # Global removal is a cache eviction; the project's declarations are left
            # untouched. Just hint when this project still references the repo.
            declared = repos.project_artifacts_for_repo(project_root, alias) if project_root else []
            note = f" — {len(declared)} artifact(s) still declared here" if declared else ""
            self.app.notify(f"removed {alias}{note}")
            self._populate()

        self.app.push_screen(
            ConfirmModal(f"Remove repo {alias!r} and wipe its cache?"),
            _on_confirm,
        )

    def _status(self, msg: str) -> None:
        """Write a message to the status line."""
        self.query_one("#status", Static).update(msg)

    def _notify_or_status(self, msg: str) -> None:
        """Notify to add a repo when none exist, otherwise show the status message.

        Args:
            msg: The status message to display when the table has rows.
        """
        if self.query_one(DataTable).row_count == 0:
            self.app.notify("add a repo first (press [a])", severity="warning")
        else:
            self._status(msg)


def _humanize(seconds: float) -> str:
    """Format an elapsed-seconds duration as a coarse "N{unit} ago" string.

    Args:
        seconds: Elapsed time in seconds; negative values are clamped to zero.

    Returns:
        A relative-time label in seconds, minutes, hours, or days.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"
