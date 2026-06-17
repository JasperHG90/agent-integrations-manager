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
    parts: list[str] = []
    if "skill" in kinds:
        parts.append("skills")
    if "agent" in kinds:
        parts.append("subagents")
    if "rules" in kinds:
        parts.append("rules")
    return " + ".join(parts) if parts else "—"


class ReposScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("a", "add_repo", "Add"),
        ("r", "refresh_current", "Refresh"),
        ("x", "remove_current", "Remove"),
        ("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Registered Repos", id="title", markup=False)
        yield DataTable(id="repos-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[a] Add  [r] Refresh  [x] Remove  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("alias", "url", "head", "last fetched", "contains")
        self._populate()
        table.focus()

    def on_screen_resume(self) -> None:
        self._populate()

    def _populate(self) -> None:
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
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def action_add_repo(self) -> None:
        self.app.push_screen(RepoAddModal(), self._on_add)

    def _on_add(self, result: RepoAddResult | None) -> None:
        if result is None:
            return
        self._status(f"adding {result.alias}…")
        self._adding = result
        self.run_worker(self._do_add_thread, exclusive=True, thread=True)

    def _do_add_thread(self) -> None:
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
        alias = self._selected_alias()
        if alias is None:
            self._notify_or_status("no row selected")
            return
        self._status(f"refreshing {alias}…")
        self._refreshing = alias
        self.run_worker(self._do_refresh_thread, exclusive=True, thread=True)

    def _do_refresh_thread(self) -> None:
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
        alias = self._selected_alias()
        if alias is None:
            self._notify_or_status("no row selected")
            return

        def _on_confirm(yes: bool | None) -> None:
            if yes is not True:
                return
            try:
                repos.remove(alias)
            except repos.RepoNotFoundError as exc:
                self.app.notify(f"remove failed: {exc}", severity="error")
                return
            self.app.notify(f"removed {alias}")
            self._populate()

        self.app.push_screen(
            ConfirmModal(f"Remove repo {alias!r} and wipe its cache?"),
            _on_confirm,
        )

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _notify_or_status(self, msg: str) -> None:
        if self.query_one(DataTable).row_count == 0:
            self.app.notify("add a repo first (press [a])", severity="warning")
        else:
            self._status(msg)


def _humanize(seconds: float) -> str:
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
