"""Project view: installed skills, agents, and MCP servers with drift detection.

Tabbed layout: Skills / Agents / MCP Servers. Action keys operate on the
active tab.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from agent_init.core import (
    agent_install,
    hashing,
    layout_profiles,
    manifest,
    mcp_install,
    mcp_registry,
    paths,
)
from agent_init.core import install as skill_install
from agent_init.core import rules as rules_mod
from agent_init.core.models import InstalledAgent, InstalledMcpServer, InstalledSkill
from agent_init.tui.modals.confirm import ConfirmModal


class ProjectScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("u", "update_current", "Update"),
        ("r", "rollback_current", "Rollback"),
        ("x", "delete_current", "Delete"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self._project_root = project_root or Path.cwd()
        self._has_manifest: bool = False
        self.last_status: str = ""

    def compose(self) -> ComposeResult:
        manifest_path = paths.project_manifest_path(self._project_root)
        yield Static(
            f"Project: {self._project_root}    ·    manifest: {manifest_path}",
            id="title",
            markup=False,
        )
        with TabbedContent(initial="skills"):
            with TabPane("Skills", id="skills"):
                yield DataTable(id="skills-table", cursor_type="row")
            with TabPane("Agents", id="agents"):
                yield DataTable(id="agents-table", cursor_type="row")
            with TabPane("MCP servers", id="mcp"):
                yield DataTable(id="mcp-table", cursor_type="row")
            with TabPane("Rules", id="rules"):
                yield DataTable(id="rules-table", cursor_type="row")
        yield Static("", id="status", markup=False)
        yield Static(
            "[u] Update  [r] Rollback  [x] Delete  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    def on_mount(self) -> None:
        for table_id in ("skills-table", "agents-table", "mcp-table", "rules-table"):
            table = self.query_one(f"#{table_id}", DataTable)
            if table_id == "skills-table":
                table.add_columns("skill", "version", "target", "drift")
            elif table_id == "agents-table":
                table.add_columns("agent", "version", "target", "drift")
            elif table_id == "mcp-table":
                table.add_columns("alias", "registry", "version", "drift")
            else:
                table.add_columns("rule", "source", "drift")
        self._populate()
        self.query_one("#skills-table", DataTable).focus()

    def on_screen_resume(self) -> None:
        self._populate()

    def _load_manifest(self) -> manifest.Manifest | None:
        try:
            m = manifest.load(self._project_root)
            self._has_manifest = True
            return m
        except manifest.ManifestNotFoundError:
            self._has_manifest = False
            return None

    def _populate(self) -> None:
        m = self._load_manifest()
        if m is None:
            self._status("no .agent-init/manifest.json — run init from the main menu")
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
        for rule_name in m.rules:
            drift, source = self._rule_drift(rule_name)
            rules_table.add_row(
                rule_name,
                source,
                drift,
                key=rule_name,
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

        dialect = f" · agent: {m.agent_dialect}" if m.agent_dialect else ""
        self._status(
            f"{len(m.skills)} skill(s), {len(m.agents)} agent(s), "
            f"{len(m.mcp_servers)} MCP server(s), {len(m.rules)} rule(s){dialect}"
        )

    def _selected_in(self, table_id: str) -> str | None:
        table = self.query_one(table_id, DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _skill_drift(self, s: InstalledSkill, target: Path | None) -> str:
        if target is None:
            return "invalid path"
        if s.content_hash is None:
            return "(no hash)"
        if not target.exists():
            return "missing"
        return "clean" if hashing.hash_tree(target) == s.content_hash else "edited"

    def _agent_drift(self, a: InstalledAgent, target: Path | None) -> str:
        if target is None:
            return "invalid path"
        if a.content_hash is None:
            return "(no hash)"
        if not target.exists():
            return "missing"
        return "clean" if hashing.hash_text(target.read_text(encoding="utf-8")) == a.content_hash else "edited"

    def _mcp_drift(self, mc: InstalledMcpServer, servers: object) -> str:
        if not isinstance(servers, dict) or mc.alias not in servers:
            return "missing"
        current_hash = hashing.hash_text(
            mcp_registry._canonical_json(servers[mc.alias])
        )
        return "clean" if current_hash == mc.entry_hash else "edited"

    def _rule_drift(self, rule_name: str) -> tuple[str, str]:
        profile = layout_profiles.resolve_active(self._project_root)
        target = paths.safe_project_path(
            self._project_root, f"{profile.rules_dir}/{rule_name}.md"
        )
        if target is None or not target.exists():
            return "missing", "—"
        try:
            expected = rules_mod.get(rule_name)
        except rules_mod.RuleNotFoundError:
            return "unknown source", "—"
        current = target.read_text(encoding="utf-8")
        drift = "clean" if current == expected.body else "edited"
        return drift, expected.source

    def _active_table(self) -> DataTable:
        active = self.query_one(TabbedContent).active
        if active == "agents":
            return self.query_one("#agents-table", DataTable)
        if active == "mcp":
            return self.query_one("#mcp-table", DataTable)
        if active == "rules":
            return self.query_one("#rules-table", DataTable)
        return self.query_one("#skills-table", DataTable)

    def _selected(self) -> str | None:
        table = self._active_table()
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key and row_key.value is not None else None

    def _guard(self) -> str | None:
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
        active = self.query_one(TabbedContent).active
        return str(active) if active else "skills"

    def action_update_current(self) -> None:
        key = self._guard()
        if key is None:
            return
        kind = self._active_kind()
        if kind == "rules":
            self.app.notify("rules are updated by re-running init or editing rules", severity="information")
            return
        if kind == "skills":
            self._update_skill(key)
        elif kind == "agents":
            self._update_agent(key)
        elif kind == "mcp":
            self._update_mcp(key)

    def _update_skill(self, qn: str) -> None:
        try:
            result = skill_install.update(self._project_root, qn)
        except skill_install.LocalEditsError as exc:
            def _on_confirm(yes: bool | None) -> None:
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
        try:
            result = agent_install.update(self._project_root, qn)
        except agent_install.AgentLocalEditsError as exc:
            def _on_confirm(yes: bool | None) -> None:
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
        try:
            result = mcp_install.update(self._project_root, alias)
        except mcp_install.McpLocalEditsError as exc:
            def _on_confirm(yes: bool | None) -> None:
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
        key = self._guard()
        if key is None:
            return
        kind = self._active_kind()
        if kind == "rules":
            self.app.notify("rules have no rollback; re-run init to refresh them", severity="information")
            return
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

    def action_delete_current(self) -> None:
        key = self._guard()
        if key is None:
            return
        kind = self._active_kind()
        if kind == "rules":
            self.app.notify("remove rules from the manifest via init/config, not here", severity="information")
            return

        def _on_confirm(yes: bool | None) -> None:
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
                self.app.notify(f"delete failed: {exc}", severity="error")
                return
            self.app.notify(f"deleted {key}")
            self._populate()

        self.app.push_screen(ConfirmModal(f"Delete {kind} {key!r}?"), _on_confirm)

    def _status(self, msg: str) -> None:
        self.last_status = msg
        self.query_one("#status", Static).update(msg)
