"""Template builder screen: create a project template from scratch.

Users name the template and add skills, agents, rules, and MCP servers via
searchable picker modals. The result is saved as a reusable `Profile`.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Static

from agent_init.core import profiles as profiles_mod
from agent_init.tui.modals.agent_picker import AgentPick, AgentPickerModal
from agent_init.tui.modals.export_toml import ExportTomlModal, ExportTomlResult
from agent_init.tui.modals.import_toml import ImportTomlModal, ImportTomlResult
from agent_init.tui.modals.mcp_picker import McpPick, McpPickerModal
from agent_init.tui.modals.rule_picker import RulePick, RulePickerModal
from agent_init.tui.modals.skill_picker import SkillPick, SkillPickerModal


class TemplateBuilderScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("s", "add_skill", "Add skill"),
        ("a", "add_agent", "Add agent"),
        ("r", "add_rule", "Add rule"),
        ("m", "add_mcp", "Add MCP"),
        ("x", "remove_selected", "Remove"),
        ("u", "import_toml", "Import TOML"),
        ("e", "export_toml", "Export TOML"),
        Binding("ctrl+s", "save", "Save", priority=True),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, profile: profiles_mod.Profile | None = None) -> None:
        super().__init__()
        if profile is not None:
            self._name = profile.name
            self._skills = list(profile.skills)
            self._agents = list(profile.agents)
            self._rules = list(profile.rules)
            self._mcp_servers = list(profile.mcp_servers)
        else:
            self._name = ""
            self._skills: list[profiles_mod.ProfileSkill] = []
            self._agents: list[profiles_mod.ProfileAgent] = []
            self._rules: list[str] = []
            self._mcp_servers: list[profiles_mod.ProfileMcpServer] = []

    def _profile(self) -> profiles_mod.Profile:
        return profiles_mod.Profile(
            name=self._name,
            skills=self._skills,
            agents=self._agents,
            rules=self._rules,
            mcp_servers=self._mcp_servers,
        )

    def compose(self) -> ComposeResult:
        yield Static("Template builder", id="title", markup=False)
        yield VerticalScroll(
            Static("Template name:", markup=False),
            Input(value=self._name, id="name"),
            Horizontal(
                Vertical(
                    Static("Skills", classes="modal-title", markup=False),
                    DataTable(id="skills-table", cursor_type="row"),
                    Button("Add skill (s)", id="add-skill"),
                    classes="builder-section",
                ),
                Vertical(
                    Static("Agents", classes="modal-title", markup=False),
                    DataTable(id="agents-table", cursor_type="row"),
                    Button("Add agent (a)", id="add-agent"),
                    classes="builder-section",
                ),
                classes="builder-row",
            ),
            Horizontal(
                Vertical(
                    Static("Rules", classes="modal-title", markup=False),
                    DataTable(id="rules-table", cursor_type="row"),
                    Button("Add rule (r)", id="add-rule"),
                    classes="builder-section",
                ),
                Vertical(
                    Static("MCP servers", classes="modal-title", markup=False),
                    DataTable(id="mcp-table", cursor_type="row"),
                    Button("Add MCP (m)", id="add-mcp"),
                    classes="builder-section",
                ),
                classes="builder-row",
            ),
            Static("", id="status", markup=False),
            Static(
                "[s] Add skill  [a] Add agent  [r] Add rule  [m] Add MCP  "
                "[x] Remove  [u] Import TOML  [e] Export TOML  "
                "[ctrl+s] Save  [b] Back  [q] Quit",
                id="hint",
                markup=False,
            ),
            classes="modal-scroll",
        )

    def on_mount(self) -> None:
        self._populate_all()
        self.query_one("#name", Input).focus()
        self.query_one("#status", Static).update("edit template contents · [enter] to add in pickers")

    def _populate_all(self) -> None:
        self._populate_skills()
        self._populate_agents()
        self._populate_rules()
        self._populate_mcp()
        self._status("edit template contents")

    def _populate_skills(self) -> None:
        table = self.query_one("#skills-table", DataTable)
        table.clear()
        table.add_columns("qualified name")
        for s in self._skills:
            table.add_row(s.qualified_name, key=s.qualified_name)

    def _populate_agents(self) -> None:
        table = self.query_one("#agents-table", DataTable)
        table.clear()
        table.add_columns("qualified name")
        for a in self._agents:
            table.add_row(a.qualified_name, key=a.qualified_name)

    def _populate_rules(self) -> None:
        table = self.query_one("#rules-table", DataTable)
        table.clear()
        table.add_columns("name")
        for r in self._rules:
            table.add_row(r, key=r)

    def _populate_mcp(self) -> None:
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        table.add_columns("registry", "alias")
        for m in self._mcp_servers:
            table.add_row(m.registry_name, m.alias, key=m.alias)

    def action_add_skill(self) -> None:
        self.app.push_screen(SkillPickerModal(), self._on_skill_picked)

    def _on_skill_picked(self, result: SkillPick | None) -> None:
        if result is None:
            return
        if any(s.qualified_name == result.qualified_name for s in self._skills):
            self.app.notify(f"skill {result.qualified_name!r} already in template", severity="warning")
            return
        self._skills.append(profiles_mod.ProfileSkill(qualified_name=result.qualified_name))
        self._populate_skills()

    def action_add_agent(self) -> None:
        self.app.push_screen(AgentPickerModal(), self._on_agent_picked)

    def _on_agent_picked(self, result: AgentPick | None) -> None:
        if result is None:
            return
        if any(a.qualified_name == result.qualified_name for a in self._agents):
            self.app.notify(f"agent {result.qualified_name!r} already in template", severity="warning")
            return
        self._agents.append(profiles_mod.ProfileAgent(qualified_name=result.qualified_name))
        self._populate_agents()

    def action_add_rule(self) -> None:
        self.app.push_screen(RulePickerModal(), self._on_rule_picked)

    def _on_rule_picked(self, result: RulePick | None) -> None:
        if result is None:
            return
        if result.name in self._rules:
            self.app.notify(f"rule {result.name!r} already in template", severity="warning")
            return
        self._rules.append(result.name)
        self._populate_rules()

    def action_add_mcp(self) -> None:
        self.app.push_screen(McpPickerModal(), self._on_mcp_picked)

    def _on_mcp_picked(self, result: McpPick | None) -> None:
        if result is None:
            return
        alias = self._default_alias(result.server.name)
        if any(m.alias == alias for m in self._mcp_servers):
            self.app.notify(
                f"alias {alias!r} already used in template; remove it and re-add",
                severity="warning",
            )
            return
        self._mcp_servers.append(
            profiles_mod.ProfileMcpServer(
                registry_name=result.server.name,
                alias=alias,
            )
        )
        self._populate_mcp()

    @staticmethod
    def _default_alias(name: str) -> str:
        short = name.split("/")[-1]
        short = short.split(":")[0]
        return "".join(c if c.isalnum() or c in "_-" else "-" for c in short).lower()

    def action_remove_selected(self) -> None:
        focused = self.app.focused
        if focused is None:
            return
        table_id = focused.id
        if table_id == "skills-table":
            self._remove_from_table("#skills-table", self._skills, "qualified_name")
        elif table_id == "agents-table":
            self._remove_from_table("#agents-table", self._agents, "qualified_name")
        elif table_id == "rules-table":
            self._remove_from_table("#rules-table", self._rules, None)
        elif table_id == "mcp-table":
            self._remove_from_table("#mcp-table", self._mcp_servers, "alias")

    def _remove_from_table(
        self,
        selector: str,
        items: list,
        attr: str | None,
    ) -> None:
        table = self.query_one(selector, DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        key = str(row_key.value) if row_key and row_key.value is not None else None
        if key is None:
            return
        if attr is None:
            items.remove(key) if key in items else None
        else:
            items[:] = [item for item in items if getattr(item, attr) != key]
        self._populate_all()

    def action_import_toml(self) -> None:
        self.app.push_screen(ImportTomlModal(), self._on_import)

    def _on_import(self, result: ImportTomlResult | None) -> None:
        if result is None:
            return
        self._name = result.profile.name
        self._skills = list(result.profile.skills)
        self._agents = list(result.profile.agents)
        self._rules = list(result.profile.rules)
        self._mcp_servers = list(result.profile.mcp_servers)
        self.query_one("#name", Input).value = self._name
        self._populate_all()
        self.app.notify(f"imported template from {result.path}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "name":
            self.action_save()
            return

    def action_export_toml(self) -> None:
        profile = self._profile()
        self.app.push_screen(
            ExportTomlModal(profile, initial_path=f"{profile.name or 'template'}.toml"),
            self._on_export,
        )

    def _on_export(self, result: ExportTomlResult | None) -> None:
        if result is None:
            return
        self.app.notify(f"exported template to {result.path}")

    def action_save(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._error("template name is required")
            return
        self._name = name
        try:
            profiles_mod.save(self._profile())
        except Exception as exc:
            self._error(f"save failed: {exc}")
            return
        self.app.notify(f"saved template {name!r}")
        self.app.pop_screen()

    def _error(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
        self.app.notify(msg, severity="error", title="Template builder")

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-skill":
            self.action_add_skill()
        elif event.button.id == "add-agent":
            self.action_add_agent()
        elif event.button.id == "add-rule":
            self.action_add_rule()
        elif event.button.id == "add-mcp":
            self.action_add_mcp()
