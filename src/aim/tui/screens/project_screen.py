"""Project view: installed skills, agents, and MCP servers with drift detection.

Tabbed layout: Skills / Agents / MCP Servers. Action keys operate on the
active tab.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from aim.core import (
    agent_install,
    hashing,
    layout_profiles,
    manifest,
    mcp_install,
    mcp_registry,
    paths,
)
from aim.core import (
    install as skill_install,
)
from aim.core import (
    prune as prune_mod,
)
from aim.core import risk as risk_mod
from aim.core import (
    sync as sync_mod,
)
from aim.core.models import InstalledAgent, InstalledMcpServer, InstalledRule, InstalledSkill
from aim.tui.modals.confirm import ConfirmModal


class ProjectScreen(Screen[None]):
    """Show installed skills, agents, MCP servers, and rules with drift status."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("y", "sync_project", "Sync"),
        ("p", "prune_project", "Prune"),
        ("u", "update_current", "Update"),
        ("r", "rollback_current", "Rollback"),
        ("x", "uninstall_current", "Uninstall"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialize the screen for the given project root.

        Args:
            project_root: Project directory to inspect; defaults to the current
                working directory.
        """
        super().__init__()
        self._project_root = project_root or Path.cwd()
        self._has_manifest: bool = False
        self.last_status: str = ""

    def compose(self) -> ComposeResult:
        """Build the title, tabbed tables, status line, and key hints."""
        lock_path = paths.project_lock_path(self._project_root)
        yield Static(
            f"Project: {self._project_root}    ·    lock: {lock_path}",
            id="title",
            markup=False,
        )
        with TabbedContent(initial="skills"):
            with TabPane("Skills", id="skills"):
                yield DataTable(id="skills-table", cursor_type="row")
            with TabPane("Subagents", id="agents"):
                yield DataTable(id="agents-table", cursor_type="row")
            with TabPane("MCP servers", id="mcp"):
                yield DataTable(id="mcp-table", cursor_type="row")
            with TabPane("Rules", id="rules"):
                yield DataTable(id="rules-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[y] Sync  [p] Prune  [u] Update  [r] Rollback  [x] Uninstall  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        """Add columns to each table, populate rows, and focus the skills table."""
        for table_id in ("skills-table", "agents-table", "mcp-table", "rules-table"):
            table = self.query_one(f"#{table_id}", DataTable)
            if table_id == "skills-table":
                table.add_columns("skill", "version", "target", "drift")
            elif table_id == "agents-table":
                table.add_columns("subagent", "version", "target", "drift")
            elif table_id == "mcp-table":
                table.add_columns("alias", "registry", "version", "drift")
            else:
                table.add_columns("rule", "source", "drift")
        self._populate()
        self.query_one("#skills-table", DataTable).focus()

    def on_screen_resume(self) -> None:
        """Refresh the tables when the screen becomes active again."""
        self._populate()

    def _load_manifest(self) -> manifest.Manifest | None:
        """Load the project manifest, tracking whether one exists.

        Returns:
            The loaded manifest, or None if the project has no lock file.
        """
        try:
            m = manifest.load(self._project_root)
            self._has_manifest = True
            return m
        except manifest.ManifestNotFoundError:
            self._has_manifest = False
            return None

    def _populate(self) -> None:
        """Rebuild every table from the manifest, preserving cursor selection."""
        m = self._load_manifest()
        if m is None:
            self._status("no aim.lock.toml — run init or sync from the main menu")
            return

        skills_table = self.query_one("#skills-table", DataTable)
        skills_key = self._selected_in("#skills-table")
        skills_table.clear()
        for s in m.skills:
            target = paths.safe_project_path(self._project_root, s.target_dir)
            drift = self._skill_drift(s, target)
            skills_table.add_row(
                s.qualified_name,
                s.current.identifier(),
                s.target_dir,
                drift,
                key=s.qualified_name,
            )

        agents_table = self.query_one("#agents-table", DataTable)
        agents_key = self._selected_in("#agents-table")
        agents_table.clear()
        for a in m.agents:
            target = paths.safe_project_path(self._project_root, a.target_path)
            drift = self._agent_drift(a, target)
            agents_table.add_row(
                a.qualified_name,
                a.current.identifier(),
                a.target_path,
                drift,
                key=a.qualified_name,
            )

        mcp_table = self.query_one("#mcp-table", DataTable)
        mcp_key = self._selected_in("#mcp-table")
        mcp_table.clear()
        try:
            mcp_data = mcp_registry.read_mcp_json(self._project_root)
            servers = mcp_data.get("mcpServers", {})
        except mcp_registry.McpRegistryError as exc:
            mcp_data = None
            servers = {}
            self.app.notify(f".mcp.json is invalid: {exc}", severity="error")
        for mc in m.mcp_servers:
            drift = self._mcp_drift(mc, servers)
            mcp_table.add_row(
                mc.alias,
                mc.registry_name,
                mc.current.registry_version or "?",
                drift,
                key=mc.alias,
            )

        rules_table = self.query_one("#rules-table", DataTable)
        rules_key = self._selected_in("#rules-table")
        rules_table.clear()
        for rule in m.rules:
            drift, source = self._rule_drift(rule)
            rules_table.add_row(
                rule.qualified_name,
                source,
                drift,
                key=rule.qualified_name,
            )

        for table_id, key in (
            ("#skills-table", skills_key),
            ("#agents-table", agents_key),
            ("#mcp-table", mcp_key),
            ("#rules-table", rules_key),
        ):
            if key is not None:
                table = self.query_one(table_id, DataTable)
                try:
                    table.move_cursor(row=table.get_row_index(key), animate=False)
                except Exception:
                    pass

        self._status(
            f"{len(m.skills)} skill(s), {len(m.agents)} subagent(s), "
            f"{len(m.mcp_servers)} MCP server(s), {len(m.rules)} rule(s)"
        )

    def _selected_in(self, table_id: str) -> str | None:
        """Return the row key currently selected in the given table.

        Args:
            table_id: CSS selector of the target DataTable.

        Returns:
            The selected row key, or None if the table is empty or has no
            selection.
        """
        table = self.query_one(table_id, DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _skill_drift(self, s: InstalledSkill, target: Path | None) -> str:
        """Compute the drift status for an installed skill.

        Args:
            s: The recorded installed skill.
            target: Resolved on-disk path of the skill, or None if invalid.

        Returns:
            A drift label such as "clean", "edited", "missing", or "invalid path".
        """
        if target is None:
            return "invalid path"
        if s.content_hash is None:
            return "(no hash)"
        if not target.exists():
            return "missing"
        return "clean" if hashing.hash_tree(target) == s.content_hash else "edited"

    def _agent_drift(self, a: InstalledAgent, target: Path | None) -> str:
        """Compute the drift status for an installed agent.

        Args:
            a: The recorded installed agent.
            target: Resolved on-disk path of the agent file, or None if invalid.

        Returns:
            A drift label such as "clean", "edited", "missing", or "invalid path".
        """
        if target is None:
            return "invalid path"
        if a.content_hash is None:
            return "(no hash)"
        if not target.exists():
            return "missing"
        return (
            "clean"
            if hashing.hash_text(target.read_text(encoding="utf-8")) == a.content_hash
            else "edited"
        )

    def _mcp_drift(self, mc: InstalledMcpServer, servers: object) -> str:
        """Compute the drift status for an installed MCP server.

        Args:
            mc: The recorded installed MCP server.
            servers: The mcpServers mapping parsed from .mcp.json.

        Returns:
            A drift label of "clean", "edited", or "missing".
        """
        if not isinstance(servers, dict) or mc.alias not in servers:
            return "missing"
        current_hash = hashing.hash_text(mcp_registry._canonical_json(servers[mc.alias]))
        return "clean" if current_hash == mc.entry_hash else "edited"

    def _rule_drift(self, rule: InstalledRule) -> tuple[str, str]:
        """Compute the drift status and source for an installed rule.

        Args:
            rule: The recorded installed rule.

        Returns:
            A tuple of (drift label, source alias). The drift label is "inline"
            when the active profile renders rules into AGENTS.md rather than
            per-rule files.
        """
        profile = layout_profiles.resolve_active(self._project_root)
        source = rule.repo_alias
        if profile.rules_mode != "files":
            # Inline-mode rules render into AGENTS.md; there is no per-rule file.
            return "inline", source
        rule_name = rule.qualified_name.split("/", 1)[-1]
        target = paths.safe_project_path(self._project_root, f"{profile.rules_dir}/{rule_name}.md")
        if target is None or not target.exists():
            return "missing", source
        if rule.content_hash is None:
            return "(no hash)", source
        current = hashing.hash_text(target.read_text(encoding="utf-8"))
        drift = "clean" if current == rule.content_hash else "edited"
        return drift, source

    def _active_table(self) -> DataTable:
        """Return the DataTable belonging to the currently active tab."""
        active = self.query_one(TabbedContent).active
        if active == "agents":
            return self.query_one("#agents-table", DataTable)
        if active == "mcp":
            return self.query_one("#mcp-table", DataTable)
        if active == "rules":
            return self.query_one("#rules-table", DataTable)
        return self.query_one("#skills-table", DataTable)

    def _selected(self) -> str | None:
        """Return the row key selected in the active tab's table, or None."""
        table = self._active_table()
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _guard(self) -> str | None:
        """Validate that an action can run on the active tab's selection.

        Returns:
            The selected row key, or None if there is no manifest or no row
            selected (notifying the user as appropriate).
        """
        if not self._has_manifest:
            self.app.notify("no manifest in this project — run init first", severity="warning")
            return None
        qn = self._selected()
        if qn is None:
            if self._active_table().row_count == 0:
                self.app.notify("nothing installed in this tab", severity="warning")
            else:
                self._status("no row selected")
            return None
        return qn

    def _active_kind(self) -> str:
        """Return the active tab identifier, defaulting to "skills"."""
        active = self.query_one(TabbedContent).active
        return str(active) if active else "skills"

    def action_sync_project(self) -> None:
        """Start a background sync of the whole project."""
        if not self._has_manifest:
            self.app.notify("no aim.lock.toml in this project — run init first", severity="warning")
            return
        self.run_worker(self._do_sync_thread, exclusive=True, thread=True)

    def _do_sync_thread(self) -> None:
        """Run the async sync in a worker thread and report results to the UI."""
        import asyncio

        try:
            result = asyncio.run(
                sync_mod.run(
                    sync_mod.SyncOptions(
                        project_root=self._project_root,
                        progress_callback=lambda kind, name, status: self.app.call_from_thread(
                            self.app.notify, f"{kind} {name}: {status}", title="Sync"
                        ),
                    )
                )
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"sync failed: {exc}", severity="error")
            return
        finally:
            for warn in risk_mod.take_risk_warnings():
                self.app.call_from_thread(self.app.notify, warn, severity="warning", title="risk")
        self.app.call_from_thread(
            self.app.notify,
            f"synced {len(result.synced_skills)} skills, "
            f"{len(result.synced_agents)} agents, "
            f"{len(result.synced_mcp)} mcp servers",
            title="Sync complete",
        )
        for warn in result.drift_warnings:
            self.app.call_from_thread(self.app.notify, warn, severity="warning")
        for err in result.repo_errors:
            self.app.call_from_thread(self.app.notify, err, severity="error")
        self.app.call_from_thread(self._populate)

    def action_prune_project(self) -> None:
        """Start planning a prune of orphaned project artifacts."""
        if not self._has_manifest:
            self.app.notify("no aim.lock.toml in this project — run init first", severity="warning")
            return
        self.run_worker(self._plan_prune_thread, exclusive=True, thread=True)

    def _plan_prune_thread(self) -> None:
        """Plan a prune in a worker thread and prompt for confirmation."""
        try:
            plan_result = prune_mod.plan(prune_mod.PruneOptions(project_root=self._project_root))
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"prune failed: {exc}", severity="error")
            return
        removals = [i for i in plan_result.removed if i.action == "would-remove"]
        if not removals:
            self.app.call_from_thread(self.app.notify, "Nothing to prune.", title="Prune")
            return
        summary = "\n".join(f"{i.kind}: {i.path}" for i in removals)
        for warn in plan_result.warnings:
            self.app.call_from_thread(self.app.notify, warn, severity="warning")
        self.app.call_from_thread(
            self.app.push_screen,
            ConfirmModal(
                f"Remove {len(removals)} item(s)?\n\n{summary}",
                confirm_label="Prune",
            ),
            self._on_prune_confirm,
        )

    def _on_prune_confirm(self, confirmed: bool | None) -> None:
        """Run the prune in a worker thread once the user confirms.

        Args:
            confirmed: Result from the confirmation modal.
        """
        if confirmed is not True:
            return
        self.run_worker(self._do_prune_thread, exclusive=True, thread=True)

    def _do_prune_thread(self) -> None:
        """Re-plan and apply the prune in a worker thread, reporting results."""
        try:
            plan_result = prune_mod.plan(prune_mod.PruneOptions(project_root=self._project_root))
            removals = [i for i in plan_result.removed if i.action == "would-remove"]
            if not removals:
                self.app.call_from_thread(self.app.notify, "Nothing to prune.", title="Prune")
                return
            result = prune_mod.apply(
                prune_mod.PruneOptions(project_root=self._project_root, force=True),
                plan_result,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"prune failed: {exc}", severity="error")
            return
        for item in result.removed:
            self.app.call_from_thread(self.app.notify, f"{item.action} {item.path}", title="Pruned")
        self.app.call_from_thread(
            self.app.notify,
            f"pruned {len(result.removed)} items, kept {len(result.kept)}",
            title="Prune complete",
        )
        self.app.call_from_thread(self._populate)

    def action_update_current(self) -> None:
        """Update the selected artifact, dispatching by the active tab kind."""
        key = self._guard()
        if key is None:
            return
        kind = self._active_kind()
        if kind == "rules":
            self.app.notify(
                "rules are updated by re-running init or editing rules", severity="information"
            )
            return
        if kind == "skills":
            self._update_skill(key)
        elif kind == "agents":
            self._update_agent(key)
        elif kind == "mcp":
            self._update_mcp(key)

    def _update_skill(self, qn: str) -> None:
        """Update a skill, prompting to force through local edits if needed.

        Args:
            qn: Qualified name of the skill to update.
        """
        try:
            result = skill_install.update(self._project_root, qn)
            assert not isinstance(result, skill_install.UpdatePreview)
        except skill_install.LocalEditsError as exc:

            def _on_confirm(yes: bool | None) -> None:
                """Force the update when the user confirms overwriting edits."""
                if yes is not True:
                    return
                try:
                    skill_install.update(self._project_root, qn, force=True)
                except Exception as inner_exc:
                    self.app.notify(f"update failed: {inner_exc}", severity="error")
                    return
                self.app.notify(f"updated {qn} (forced)")
                self._populate()

            self.app.push_screen(
                ConfirmModal(f"{exc}\n\nOverwrite local edits?", confirm_label="Force update"),
                _on_confirm,
            )
            return
        except Exception as exc:
            self.app.notify(f"update failed: {exc}", severity="error")
            return
        self.app.notify(f"updated {qn} -> {result.current.identifier()}")
        self._populate()

    def _update_agent(self, qn: str) -> None:
        """Update an agent, prompting to force through local edits if needed.

        Args:
            qn: Qualified name of the agent to update.
        """
        try:
            result = agent_install.update(self._project_root, qn)
        except agent_install.AgentLocalEditsError as exc:

            def _on_confirm(yes: bool | None) -> None:
                """Force the update when the user confirms overwriting edits."""
                if yes is not True:
                    return
                try:
                    agent_install.update(self._project_root, qn, force=True)
                except Exception as inner_exc:
                    self.app.notify(f"update failed: {inner_exc}", severity="error")
                    return
                self.app.notify(f"updated {qn} (forced)")
                self._populate()

            self.app.push_screen(
                ConfirmModal(f"{exc}\n\nOverwrite local edits?", confirm_label="Force update"),
                _on_confirm,
            )
            return
        except Exception as exc:
            self.app.notify(f"update failed: {exc}", severity="error")
            return
        self.app.notify(f"updated {qn} -> {result.current.identifier()}")
        self._populate()

    def _update_mcp(self, alias: str) -> None:
        """Update an MCP server, prompting to force through local edits if needed.

        Args:
            alias: Alias of the MCP server to update.
        """
        try:
            result = mcp_install.update(self._project_root, alias)
        except mcp_install.McpLocalEditsError as exc:

            def _on_confirm(yes: bool | None) -> None:
                """Force the update when the user confirms overwriting edits."""
                if yes is not True:
                    return
                try:
                    mcp_install.update(self._project_root, alias, force=True)
                except Exception as inner_exc:
                    self.app.notify(f"update failed: {inner_exc}", severity="error")
                    return
                self.app.notify(f"updated {alias} (forced)")
                self._populate()

            self.app.push_screen(
                ConfirmModal(f"{exc}\n\nOverwrite local edits?", confirm_label="Force update"),
                _on_confirm,
            )
            return
        except Exception as exc:
            self.app.notify(f"update failed: {exc}", severity="error")
            return
        self.app.notify(f"updated {alias} -> {result.current.registry_version or '?'}")
        self._populate()

    def action_rollback_current(self) -> None:
        """Roll the selected artifact back to its previous version."""
        key = self._guard()
        if key is None:
            return
        kind = self._active_kind()
        if kind == "rules":
            self.app.notify(
                "rules have no rollback; re-run init to refresh them", severity="information"
            )
            return
        result: InstalledSkill | InstalledAgent | InstalledMcpServer
        try:
            if kind == "skills":
                result = skill_install.rollback(self._project_root, key)
            elif kind == "agents":
                result = agent_install.rollback(self._project_root, key)
            elif kind == "mcp":
                result = mcp_install.rollback(self._project_root, key)
            else:
                return
        except Exception as exc:
            self.app.notify(f"rollback failed: {exc}", severity="error")
            return
        self.app.notify(f"rolled back {key} -> {result.current.identifier()}")
        self._populate()

    def action_uninstall_current(self) -> None:
        """Uninstall the selected artifact after user confirmation."""
        key = self._guard()
        if key is None:
            return
        kind = self._active_kind()
        if kind == "rules":
            self.app.notify(
                "remove rules from the manifest via init/config, not here", severity="information"
            )
            return

        def _on_confirm(yes: bool | None) -> None:
            """Delete the artifact when the user confirms the uninstall."""
            if yes is not True:
                return
            try:
                if kind == "skills":
                    skill_install.delete(self._project_root, key)
                elif kind == "agents":
                    agent_install.delete(self._project_root, key)
                elif kind == "mcp":
                    mcp_install.delete(self._project_root, key)
            except Exception as exc:
                self.app.notify(f"uninstall failed: {exc}", severity="error")
                return
            self.app.notify(f"uninstalled {key}")
            self._populate()

        self.app.push_screen(ConfirmModal(f"Uninstall {kind} {key!r}?"), _on_confirm)

    def _status(self, msg: str) -> None:
        """Record and display a status message in the status line.

        Args:
            msg: The message to show.
        """
        self.last_status = msg
        self.query_one("#status", Static).update(msg)
