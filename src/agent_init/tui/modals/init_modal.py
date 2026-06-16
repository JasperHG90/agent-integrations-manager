"""Modal: configure `init` for a project. Pick project root, template, and
which mirror files to write."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from agent_init.core import init as init_mod
from agent_init.core import layout_profiles, templates


@dataclass(frozen=True)
class InitConfig:
    project_root: Path
    template: str
    mirrors: tuple[str, ...]
    seed_default_rules: bool
    force: bool
    agent_dialect: str | None
    layout_profile: str | None = None


class InitModal(ModalScreen[InitConfig | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, project_root: Path | None = None) -> None:
        super().__init__()
        self._initial_project = (project_root or Path.cwd()).resolve()
        self._profile_options: list[tuple[str, str]] = []

    @staticmethod
    def _build_profile_options(project_root: Path) -> list[tuple[str, str]]:
        profiles = layout_profiles.list_profiles(project_root)
        return [(p.display_name or p.name, p.name) for p in profiles]

    @staticmethod
    def _mirror_id(name: str) -> str:
        # Textual ids must match [A-Za-z][A-Za-z0-9_-]* — filenames have dots,
        # so replace them. Used for both compose and read.
        return "mirror-" + name.replace(".", "-")

    def compose(self) -> ComposeResult:
        templates_avail = [t.name for t in templates.list_templates()]
        default_template = (
            templates.BUILTIN_DEFAULT
            if templates.BUILTIN_DEFAULT in templates_avail
            else (templates_avail[0] if templates_avail else templates.BUILTIN_DEFAULT)
        )
        self._profile_options = self._build_profile_options(self._initial_project)
        yield Vertical(
            Static("Initialize project", classes="modal-title", markup=False),
            Static("Project root:", markup=False),
            Input(value=str(self._initial_project), id="project-root"),
            Static("Template:", markup=False),
            Input(value=default_template, id="template"),
            Static("Profile:", markup=False),
            Select(self._profile_options, id="layout-profile", allow_blank=True),
            Static("Mirror files (write a copy of AGENTS.md as):", markup=False),
            *(Checkbox(name, id=self._mirror_id(name)) for name in init_mod.KNOWN_MIRRORS),
            Static("Other mirror (optional, e.g. CURSOR.md):", markup=False),
            Input(value="", placeholder="<name>.md", id="other-mirror"),
            Static("Primary agent dialect (optional, blank = none):", markup=False),
            Input(value="", placeholder="claude / gemini / opencode", id="agent-dialect"),
            Checkbox("Seed default-flagged rules", value=True, id="seed-defaults"),
            Checkbox("Force overwrite if files exist", id="force"),
            Static("", id="error", markup=False, classes="modal-error"),
            Horizontal(
                Button("Initialize", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        self.query_one("#project-root", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _error(self, msg: str, focus_id: str) -> None:
        self.query_one("#error", Static).update(msg)
        self.query_one(f"#{focus_id}", Input).focus()
        self.app.notify(msg, severity="error", title="Init")

    def _submit(self) -> None:
        project_root_str = self.query_one("#project-root", Input).value.strip()
        template = self.query_one("#template", Input).value.strip()
        if not project_root_str:
            self._error("project root is required", "project-root")
            return
        if not template:
            self._error("template name is required", "template")
            return
        mirrors_list: list[str] = [
            name
            for name in init_mod.KNOWN_MIRRORS
            if self.query_one(f"#{self._mirror_id(name)}", Checkbox).value
        ]
        other = self.query_one("#other-mirror", Input).value.strip()
        if other:
            if not init_mod.is_valid_mirror_name(other):
                self._error(
                    f"other mirror {other!r} invalid: use <name>.md, letters/numbers/_-/. only",
                    "other-mirror",
                )
                return
            if other not in mirrors_list:
                mirrors_list.append(other)
        seed = self.query_one("#seed-defaults", Checkbox).value
        force = self.query_one("#force", Checkbox).value
        dialect = self.query_one("#agent-dialect", Input).value.strip().lower() or None
        profile_value = self.query_one("#layout-profile", Select).value
        layout_profile: str | None = None
        if isinstance(profile_value, tuple):
            layout_profile = profile_value[1]
        elif profile_value is not None and profile_value not in (Select.BLANK, Select.NULL):
            layout_profile = str(profile_value)
        self.dismiss(
            InitConfig(
                project_root=Path(project_root_str).expanduser(),
                template=template,
                mirrors=tuple(mirrors_list),
                seed_default_rules=seed,
                force=force,
                agent_dialect=dialect,
                layout_profile=layout_profile,
            )
        )
