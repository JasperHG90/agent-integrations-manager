"""Layout view: layout profiles and plugin targets in one place.

Both tabs configure how a client's tooling lands in a project: a layout
profile says WHERE aim installs skills/rules/mirror files, a target teaches
aim HOW to discover and install that client's plugins. Action keys operate on
the active tab.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static, TabbedContent, TabPane

from aim.core import git, layout_profiles, manifest, repos, target_install, targets
from aim.tui import errors as tui_errors
from aim.tui.modals.busy import BusyModal
from aim.tui.modals.confirm import ConfirmModal
from aim.tui.modals.layout_profile_modal import (
    LayoutProfileModal,
    LayoutProfileResult,
)
from aim.tui.modals.repo_filter import RepoFilterModal, RepoFilterPick
from aim.tui.modals.skill_view import SkillViewModal
from aim.tui.modals.target_install import TargetInstallConfig, TargetInstallModal

_TARGET_DEPLOY_ERRORS: tuple[type[BaseException], ...] = (  # noqa: RUF005
    target_install.TargetNotIndexedError,
    target_install.TargetManifestPathEscapeError,
    manifest.ManifestNotFoundError,
    git.GitError,
) + tui_errors.GOVERNANCE_ERRORS

_PROFILES_HINT = "[a] Add  [e] Edit  [x] Delete  [s] Set active  [b] Back  [q] Quit"
_TARGETS_HINT = "[/] Search  [f] Repo filter  [enter/v] View  [i] Install  [b] Back  [q] Quit"


class LayoutScreen(Screen[None]):
    """Manage layout profiles and browse/install plugin targets in one screen."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("a", "add_profile", "Add profile"),
        ("e", "edit_profile", "Edit profile"),
        ("x", "delete_profile", "Delete profile"),
        ("s", "set_active", "Set active"),
        ("slash", "focus_search", "Search targets"),
        ("f", "pick_repo_filter", "Filter by repo"),
        ("enter", "view_current", "View target"),
        ("v", "view_current", "View target"),
        ("i", "install_current", "Install target"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialize the screen, resolving the project root.

        Args:
            project_root: Project directory; defaults to the current working directory.
        """
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()
        self._repo_filter: str | None = None
        self._installing: tuple[str, TargetInstallConfig] | None = None
        self._busy: BusyModal | None = None
        self._pending_search_focus = False
        # Row key "active:<name>" -> the loaded spec file backing that row.
        self._active_targets: dict = {}
        # Invalid-spec warnings already shown (populate runs per keystroke).
        self._reported_spec_warnings: set[str] = set()

    def compose(self) -> ComposeResult:
        """Build the title, tabbed tables, status line, and key hints."""
        yield Static("Layout", id="title", markup=False)
        with TabbedContent(initial="profiles"):
            with TabPane("Profiles", id="profiles"):
                yield Static(
                    "project = repo-only · global = DB cache + read-only repo copy",
                    id="scope-help",
                    markup=False,
                )
                yield DataTable(id="profiles-table", cursor_type="row")
            with TabPane("Targets", id="targets"):
                yield Input(placeholder="search…", id="search-bar")
                yield DataTable(id="targets-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(_PROFILES_HINT, id="hint", markup=False)

    def on_mount(self) -> None:
        """Set up both tables, populate them, and focus the profiles table."""
        profiles_table = self.query_one("#profiles-table", DataTable)
        profiles_table.add_columns(
            "active", "name", "scope", "skills_dir", "rules_dir", "subagents_md", "symlinks"
        )
        targets_table = self.query_one("#targets-table", DataTable)
        targets_table.add_columns("qualified name", "repo", "description")
        self._refresh_profiles()
        self._populate_targets("")
        profiles_table.focus()

    def on_screen_resume(self) -> None:
        """Refresh both tabs when the screen regains focus."""
        self._refresh_profiles()
        self._populate_targets(self.query_one("#search-bar", Input).value)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Swap hint + status and focus the activated tab's table (or search bar).

        Focus is set HERE, not in the action that switched tabs: this handler
        runs from a queued message, so a synchronous .focus() in the action
        would be overridden by it a moment later. The status line is re-emitted
        too so the Targets tab never shows a stale profile count (or vice versa).
        """
        if self._active_tab() == "targets":
            self.query_one("#hint", Static).update(_TARGETS_HINT)
            self._populate_targets(self.query_one("#search-bar", Input).value)
            if self._pending_search_focus:
                self._pending_search_focus = False
                self.query_one("#search-bar", Input).focus()
            else:
                self.query_one("#targets-table", DataTable).focus()
        else:
            self.query_one("#hint", Static).update(_PROFILES_HINT)
            self._refresh_profiles()
            self.query_one("#profiles-table", DataTable).focus()

    def _active_tab(self) -> str:
        """Return the active tab identifier, defaulting to "profiles"."""
        active = self.query_one(TabbedContent).active
        return str(active) if active else "profiles"

    def _status(self, msg: str) -> None:
        """Update the status line with the given message."""
        self.query_one("#status", Static).update(msg)

    # ------------------------------------------------------------------ profiles

    def _active_name(self) -> str | None:
        """Return the active profile name from the manifest, or None if unset."""
        try:
            m = manifest.load(self._project_root)
            return m.layout_profile
        except manifest.ManifestNotFoundError:
            return None

    def _selected_profile_name(self) -> str | None:
        """Return the name of the profile under the cursor, or None if empty."""
        table = self.query_one("#profiles-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _refresh_profiles(self) -> None:
        """Sync profiles and repopulate the table, preserving the selection."""
        report = layout_profiles.sync_profiles(self._project_root)
        for warning in report.warnings:
            self.app.notify(warning, severity="warning")

        active = self._active_name()
        profiles = layout_profiles.list_profiles(self._project_root)
        table = self.query_one("#profiles-table", DataTable)
        selected = self._selected_profile_name()
        table.clear()
        for p in profiles:
            is_active = ">" if p.name == active else ""
            scope = _scope_label(p)
            table.add_row(
                is_active,
                p.display_name or p.name,
                scope,
                p.skills_dir,
                p.rules_dir,
                p.agents_md,
                ",".join(p.symlinks) if p.symlinks else "-",
                key=p.name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        self._status(f"{len(profiles)} profile(s)")

    def _needs_profiles_tab(self) -> bool:
        """Guard profile actions: hint and return True when Targets is active."""
        if self._active_tab() != "profiles":
            self._status("profile actions apply on the Profiles tab")
            return True
        return False

    def action_add_profile(self) -> None:
        """Open the modal to create a new layout profile."""
        if self._needs_profiles_tab():
            return
        self.app.push_screen(
            LayoutProfileModal(self._project_root),
            self._on_save,
        )

    def action_edit_profile(self) -> None:
        """Open the modal to edit the selected layout profile."""
        if self._needs_profiles_tab():
            return
        name = self._selected_profile_name()
        if name is None:
            self._status("select a profile to edit")
            return
        try:
            profile = layout_profiles.get_profile(self._project_root, name)
        except layout_profiles.LayoutProfileNotFoundError:
            self._status(f"profile {name!r} not found")
            return
        self.app.push_screen(
            LayoutProfileModal(self._project_root, profile=profile),
            self._on_save,
        )

    def _on_save(self, result: LayoutProfileResult | None) -> None:
        """Persist a saved profile and refresh the table.

        Args:
            result: Outcome from the profile modal, or None when cancelled.
        """
        if result is None:
            return
        try:
            if result.profile.scope == layout_profiles.LayoutProfileScope.GLOBAL:
                layout_profiles.save_global_profile(self._project_root, result.profile)
            else:
                layout_profiles.save_project_profile(self._project_root, result.profile)
        except Exception as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        # Rename: remove the old profile if the name changed.
        if result.original_name is not None and result.original_name != result.profile.name:
            layout_profiles.delete_global_profile(self._project_root, result.original_name)
        self.app.notify(f"saved profile {result.profile.name}")
        self._refresh_profiles()

    def action_delete_profile(self) -> None:
        """Confirm and delete the selected profile, refusing built-ins."""
        if self._needs_profiles_tab():
            return
        name = self._selected_profile_name()
        if name is None:
            self._status("select a profile to delete")
            return
        if name in (
            layout_profiles.BUILTIN_CLAUDE.name,
            layout_profiles.BUILTIN_GEMINI.name,
        ):
            self.app.notify("built-in profiles cannot be deleted", severity="error")
            return

        def _on_confirm(yes: bool | None) -> None:
            """Delete the profile if confirmed, clearing it from the manifest.

            Args:
                yes: Confirmation result; deletes only when True.
            """
            if yes is not True:
                return
            deleted = layout_profiles.delete_global_profile(self._project_root, name)
            if not deleted:
                self.app.notify(f"profile {name!r} not found", severity="error")
                return
            # If this was the active project profile, clear it from the manifest.
            try:
                m = manifest.load(self._project_root)
            except manifest.ManifestNotFoundError:
                m = None
            if m is not None and m.layout_profile == name:
                m.layout_profile = None
                manifest.save(self._project_root, m)
            self.app.notify(f"deleted profile {name!r}")
            self._refresh_profiles()

        self.app.push_screen(ConfirmModal(f"Delete layout profile {name!r}?"), _on_confirm)

    def action_set_active(self) -> None:
        """Activate the selected profile and refresh the table."""
        if self._needs_profiles_tab():
            return
        name = self._selected_profile_name()
        if name is None:
            self._status("select a profile to activate")
            return
        try:
            layout_profiles.set_active(self._project_root, name)
        except Exception as exc:
            self.app.notify(f"activation failed: {exc}", severity="error")
            return
        self.app.notify(f"active layout profile: {name}")
        self._refresh_profiles()

    # ------------------------------------------------------------------- targets

    def _populate_targets(self, query: str) -> None:
        """Refresh the targets table: ACTIVE target files first, then installable.

        Active rows are the target TOMLs already governing this project
        (`.aim/targets/*.toml` plus the global targets dir) — the ones
        `plugin_kinds.load_kinds` actually loads. Listing only the repo-indexed
        installables made the tab claim "no targets" while a project-local
        target was in force.
        """
        from aim.core import plugin_kinds

        table = self.query_one("#targets-table", DataTable)
        selected = self._selected_target()
        table.clear()

        # Active target files, deduped by kind name (project overrides global).
        active: dict[str, plugin_kinds.KindSpecFile] = {}
        for ksf in plugin_kinds.list_kind_specs(self._project_root):
            active[ksf.spec.name] = ksf
        # A broken spec is exactly what a user opens this tab to diagnose —
        # surface it instead of silently skipping (drain the shared channel so
        # the warnings don't pile up and later dump into an unrelated flow).
        for warn in plugin_kinds.take_load_warnings():
            if warn not in self._reported_spec_warnings:
                self._reported_spec_warnings.add(warn)
                self.app.notify(warn, severity="warning", title="invalid target spec")
        self._active_targets = {f"active:{name}": ksf for name, ksf in sorted(active.items())}
        q = query.strip().lower()
        active_rows = [
            (key, ksf)
            for key, ksf in self._active_targets.items()
            if (not q or q in ksf.spec.name.lower()) and self._repo_filter is None
        ]

        rows = targets.search(query) if query else targets.list_targets()
        if self._repo_filter is not None:
            rows = [r for r in rows if r.repo_alias == self._repo_filter]
        filter_label = f" [repo={self._repo_filter}]" if self._repo_filter else ""
        total = len(active_rows) + len(rows)
        if total == 0 and self._active_tab() == "targets":
            if not query and self._repo_filter is None:
                self._status("no targets active or indexed — add a repo that ships targets/*.toml")
            else:
                bits = []
                if query:
                    bits.append(f"{query!r}")
                if self._repo_filter:
                    bits.append(f"repo={self._repo_filter}")
                self._status("no matches for " + " ".join(bits))
            return
        for key, ksf in active_rows:
            where = "(this project)" if ksf.scope == "project" else "(global)"
            table.add_row(
                ksf.spec.name,
                where,
                f"active — {ksf.spec.manifest.file} → {ksf.spec.registration.vendor_into}",
                key=key,
            )
        for r in rows:
            table.add_row(
                r.qualified_name,
                r.repo_alias,
                (r.description or "")[:60],
                key=r.qualified_name,
            )
        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected), animate=False)
            except Exception:
                pass
        if self._active_tab() == "targets":
            self._status(f"{len(active_rows)} active, {len(rows)} installable{filter_label}")

    def _selected_target(self) -> str | None:
        """Return the qualified name of the highlighted target row, or None."""
        table = self.query_one("#targets-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _needs_targets_tab(self) -> bool:
        """Guard target actions: hint and return True when Profiles is active."""
        if self._active_tab() != "targets":
            self._status("target actions apply on the Targets tab")
            return True
        return False

    def action_pick_repo_filter(self) -> None:
        """Open a picker to filter targets by a single repo (or clear the filter)."""
        if self._needs_targets_tab():
            return
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
        self._populate_targets(self.query_one("#search-bar", Input).value)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Repopulate the targets table as the user types in the search bar."""
        if event.input.id == "search-bar":
            self._populate_targets(event.value)

    def action_focus_search(self) -> None:
        """Switch to the Targets tab (if needed) and focus the search bar."""
        tabs = self.query_one(TabbedContent)
        if tabs.active != "targets":
            # The queued TabActivated handler performs the focus; see there.
            self._pending_search_focus = True
            tabs.active = "targets"
            return
        self.query_one("#search-bar", Input).focus()

    def action_view_current(self) -> None:
        """Open the selected target's TOML in a read-only view modal."""
        if self._needs_targets_tab():
            return
        qn = self._selected_target()
        if qn is None:
            if self.query_one("#targets-table", DataTable).row_count == 0:
                self.app.notify("no targets indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        if qn in self._active_targets:
            ksf = self._active_targets[qn]
            try:
                content = ksf.path.read_text(encoding="utf-8")
            except OSError as exc:
                self.app.notify(f"view failed: {exc}", severity="error")
                return
            self.app.push_screen(SkillViewModal(str(ksf.path), content))
            return
        try:
            content = targets.read_target_content(qn)
        except targets.TargetNotIndexedError as exc:
            self.app.notify(f"view failed: {exc}", severity="error")
            return
        self.app.push_screen(SkillViewModal(qn, content))

    def action_install_current(self) -> None:
        """Prompt for install config for the selected target, then install it."""
        if self._needs_targets_tab():
            return
        qn = self._selected_target()
        if qn is None:
            if self.query_one("#targets-table", DataTable).row_count == 0:
                self.app.notify("no targets indexed — add a repo first", severity="warning")
            else:
                self._status("no row selected")
            return
        if qn in self._active_targets:
            ksf = self._active_targets[qn]
            self.app.notify(
                f"{ksf.spec.name} is already active — loaded from {ksf.path}",
                severity="information",
            )
            return
        self.app.push_screen(
            TargetInstallModal(qn, initial_project=self._project_root),
            lambda cfg: self._install(qn, cfg),
        )

    def _install(self, qualified_name: str, cfg: TargetInstallConfig | None) -> None:
        """Kick off a threaded install for a target, or no-op if config is None."""
        if cfg is None:
            return
        self._installing = (qualified_name, cfg)
        self._status(f"installing {qualified_name}…")
        busy = BusyModal(f"Installing {qualified_name}…")
        self._busy = busy
        self.app.push_screen(busy)
        self.run_worker(self._do_install_thread, exclusive=True, thread=True)

    def _do_install_thread(self) -> None:
        """Run the pending install on a worker thread and report results to the UI."""
        if self._installing is None:
            return
        qualified_name, cfg = self._installing
        try:
            result = target_install.install(
                cfg.project_root, qualified_name, pin=cfg.pin, track=cfg.track
            )
        except _TARGET_DEPLOY_ERRORS as exc:
            self.app.call_from_thread(self.app.notify, f"install failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"install failed: {exc}")
            return
        finally:
            self.app.call_from_thread(self._dismiss_busy)
        self.app.call_from_thread(
            self.app.notify,
            f"installed {qualified_name} {result.current.identifier()}",
            title="Target installed",
        )
        for warn in target_install.take_install_warnings():
            self.app.call_from_thread(self.app.notify, warn, severity="warning", title="config")
        self.app.call_from_thread(self._status, f"installed {qualified_name}")

    def _dismiss_busy(self) -> None:
        """Close the loading overlay if one is showing. Runs on the UI thread.

        Dismiss only when the overlay is the top screen: Screen.dismiss() pops
        whatever is on top, so calling it while another screen covers the
        overlay would pop THAT screen and leave the overlay stuck.
        """
        if self._busy is not None and self.app.screen is self._busy:
            self._busy.dismiss()
        self._busy = None


def _scope_label(profile: layout_profiles.LayoutProfile) -> str:
    """Return the display label for a profile's scope.

    Args:
        profile: The layout profile to label.

    Returns:
        "built-in" for built-in profiles, otherwise the scope value.
    """
    if profile.name in (layout_profiles.BUILTIN_CLAUDE.name, layout_profiles.BUILTIN_GEMINI.name):
        return "built-in"
    return profile.scope.value
