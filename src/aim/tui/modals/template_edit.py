"""Modal: edit a saved project template's editable fields.

Every selectable item is a checkbox. Checked = keep in template. Unchecked = drop.
This covers rules, skills, subagents, and MCP servers. The modal also edits the
instruction template name, layout profile, and agent dialect.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from aim.core import profiles as profiles_mod
from aim.core import repo_rules as repo_rules_mod
from aim.tui.widgets import ToggleRow


@dataclass(frozen=True)
class TemplateEditResult:
    name: str
    instruction_template: str
    layout_profile: str | None
    rules: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()


class TemplateEditModal(ModalScreen[TemplateEditResult | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Save", priority=True),
    ]

    def __init__(self, profile: profiles_mod.Profile) -> None:
        super().__init__()
        self._profile = profile
        self._rule_names: list[str] = []

    def _toggle_id(self, kind: str, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "_-" else "-" for c in key)
        return f"{kind}-{safe}"

    def compose(self) -> ComposeResult:
        self._rule_names = [r.qualified_name for r in repo_rules_mod.list_rules()]

        rule_toggles = [
            ToggleRow(
                name,
                value=name in self._profile.rules,
                id=self._toggle_id("rule", name),
            )
            for name in self._rule_names
        ]
        skill_toggles = [
            ToggleRow(
                s.qualified_name,
                value=True,
                id=self._toggle_id("skill", s.qualified_name),
            )
            for s in self._profile.skills
        ]
        agent_toggles = [
            ToggleRow(
                a.qualified_name,
                value=True,
                id=self._toggle_id("agent", a.qualified_name),
            )
            for a in self._profile.agents
        ]
        mcp_toggles = [
            ToggleRow(
                f"{m.registry_name} as {m.alias}",
                value=True,
                id=self._toggle_id("mcp", m.alias),
            )
            for m in self._profile.mcp_servers
        ]

        yield Vertical(
            Static("Edit project template", classes="modal-title", markup=False),
            VerticalScroll(
                Static("Template name:", markup=False),
                Input(value=self._profile.name, id="name"),
                Static("Instruction template:", markup=False),
                Input(value=self._profile.instruction_template, id="instruction-template"),
                Static("Layout profile:", markup=False),
                Input(value=self._profile.layout_profile or "", id="layout-profile"),
                Static("Included rules:", markup=False),
                *rule_toggles,
                Static("Included skills:", markup=False),
                *skill_toggles,
                Static("Included subagents:", markup=False),
                *agent_toggles,
                Static("Included MCP servers:", markup=False),
                *mcp_toggles,
                Static("", id="error", markup=False, classes="modal-error"),
                classes="modal-scroll",
            ),
            Horizontal(
                Button("Save", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("name", "instruction-template", "layout-profile"):
            self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self._submit()

    def _error(self, msg: str) -> None:
        self.query_one("#error", Static).update(msg)
        self.app.notify(msg, severity="error", title="Edit template")

    def _checked_keys(self, kind: str, keys: list[str]) -> list[str]:
        return [
            key for key in keys if self.query_one(f"#{self._toggle_id(kind, key)}", ToggleRow).value
        ]

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        instruction_template = self.query_one("#instruction-template", Input).value.strip()
        if not name:
            self._error("template name is required")
            return
        if not instruction_template:
            self._error("instruction template name is required")
            return
        layout_profile = self.query_one("#layout-profile", Input).value.strip() or None

        skill_keys = [s.qualified_name for s in self._profile.skills]
        agent_keys = [a.qualified_name for a in self._profile.agents]
        mcp_keys = [m.alias for m in self._profile.mcp_servers]

        self.dismiss(
            TemplateEditResult(
                name=name,
                instruction_template=instruction_template,
                layout_profile=layout_profile,
                rules=tuple(self._checked_keys("rule", self._rule_names)),
                skills=tuple(self._checked_keys("skill", skill_keys)),
                agents=tuple(self._checked_keys("agent", agent_keys)),
                mcp_servers=tuple(self._checked_keys("mcp", mcp_keys)),
            )
        )
