"""Config screen — project-level settings plus the global default template.

Provides a single, focused pane for the *current* project's manifest:
- where aim keeps its lockfile
- active layout profile (skills/rules/subagents/mcp paths)
- instruction template, symlinks
- applied rules (read-only pointer to the Rules screen)
- save re-runs `init` against the project

Also exposes an editor for the global `default.md.j2` template. Editing this
changes the scaffold used by every future `init`. Project-specific guidance
still belongs in reusable rules, not in this template.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Static, TextArea

from aim.core import declarations, layout_profiles, manifest, paths, templates
from aim.core import init as init_mod

_HELP_TEXT = (
    "Fields:\n"
    "  Instruction template — which AGENTS.md scaffold to use (stored in aim.toml).\n"
    "  Applied rules — managed on the Rules [u] screen.\n"
    "  Layout profile — controls skills/rules/subagents/mcp paths, rules mode, AND\n"
    "    per-agent AGENTS.md symlinks.\n"
    "Shortcuts work from the main menu: [l] PROFILES, [u] RULES."
)

_TEMPLATE_HELP = (
    "Edit the global default template below. This scaffold is used for every new project.\n"
    "Keep it universal — project-specific sections (e.g. Python rules) belong in reusable rules, not here."
)


class ConfigScreen(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("b", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()
        self._template_path = templates._builtin_override_path(templates.BUILTIN_DEFAULT)

    def compose(self) -> ComposeResult:
        yield Static("Config", id="title", markup=False)
        try:
            m = manifest.load(self._project_root)
            has_manifest = True
        except manifest.ManifestNotFoundError:
            m = None
            has_manifest = False

        try:
            decl = declarations.load(self._project_root)
        except declarations.DeclarationsNotFoundError:
            decl = declarations.load_or_default(self._project_root)

        active_profile = self._active_profile_label()
        instruction_template = decl.instruction_template or "default"
        applied_rules = list(m.rules) if m else list(decl.rules)

        yield Vertical(
            Static(f"Project: {self._project_root}", classes="config-paths", markup=False),
            Static(
                "lockfile: "
                + str(paths.project_lock_path(self._project_root))
                + ("" if has_manifest else "  (not initialized — save will create one)"),
                classes="config-paths",
                markup=False,
            ),
            Static(
                f"Active layout profile: {active_profile}",
                id="active-profile",
                classes="config-paths",
                markup=False,
            ),
            Static("Project root:", classes="config-heading", markup=False),
            Input(value=str(self._project_root), id="proj-root"),
            Static("Instruction template:", classes="config-heading", markup=False),
            Input(value=instruction_template, id="proj-template"),
            Static(
                f"Applied rules ({len(applied_rules)}):",
                classes="config-heading",
                markup=False,
            ),
            Static(
                ", ".join(applied_rules)
                if applied_rules
                else "(none — manage on the Rules screen)",
                id="proj-rules-display",
                classes="config-paths",
                markup=False,
            ),
            Button("Save project settings (updates aim.toml)", id="proj-save", variant="primary"),
            Static(_HELP_TEXT, classes="config-help", markup=True),
            id="project-pane",
        )

        template_body = (
            self._template_path.read_text(encoding="utf-8") if self._template_path.exists() else ""
        )
        yield Vertical(
            Static("Global default template", classes="config-heading", markup=False),
            Static(_TEMPLATE_HELP, classes="config-help", markup=True),
            TextArea(template_body, id="global-template", language="markdown"),
            Button("Save global template", id="template-save", variant="primary"),
            id="template-pane",
        )

        yield Static("", id="status", markup=False)
        yield Static(
            "[b] Back  [q] Quit  — manage profiles with [l] PROFILES",
            id="hint",
            markup=False,
        )

    def _active_profile_label(self) -> str:
        try:
            profile = layout_profiles.resolve_active(self._project_root)
        except Exception:
            return "—"
        return f"{profile.name}  ·  skills:{profile.skills_dir}  rules:{profile.rules_mode}  subagents:{profile.agents_dir}  mcp:{profile.mcp_json}"

    def on_mount(self) -> None:
        self._status("edit project settings or the global template, then save")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proj-save":
            self._save_project()
        elif event.button.id == "template-save":
            self._save_template()

    def _save_project(self) -> None:
        project = Path(self.query_one("#proj-root", Input).value).expanduser()
        instruction_template = self.query_one("#proj-template", Input).value.strip() or "default"
        try:
            result = init_mod.run(
                init_mod.InitOptions(
                    project_root=project,
                    instruction_template=instruction_template,
                )
            )
        except Exception as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        verb = "Refreshed" if result.re_init else "Initialized"
        self.app.notify(f"{verb} {result.declarations_path}", title="Project saved")
        self.app.notify("Run Lock then Sync to apply the updated declarations.")
        self._project_root = project.resolve()

    def _save_template(self) -> None:
        body = self.query_one("#global-template", TextArea).text
        try:
            self._template_path.parent.mkdir(parents=True, exist_ok=True)
            self._template_path.write_text(body, encoding="utf-8")
        except Exception as exc:
            self.app.notify(f"template save failed: {exc}", severity="error")
            return
        self.app.notify(
            f"global template saved to {self._template_path}",
            title="Template saved",
        )
        # Re-run init for the current project so the new template is applied immediately.
        self._save_project()

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
