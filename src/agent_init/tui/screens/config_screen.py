"""Config screen — project-level settings only.

Provides a single, focused pane for the *current* project's manifest:
- where agent-init keeps its manifest
- active layout profile (skills/rules/agents/mcp paths)
- template, mirrors, agent dialect
- applied rules (read-only pointer to the Rules screen)
- save re-runs `init` against the project

Global state (roots, rule-repo overlays, saved init profiles) is now managed
from the CLI; this screen no longer duplicates the Layout Profiles screen.
A Help block at the bottom explains each field and how to get more help.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Static

from agent_init.core import init as init_mod
from agent_init.core import layout_profiles, manifest, paths

_HELP_TEXT = (
    "Fields:\n"
    "  Template      — which AGENTS.md scaffold to use.\n"
    "  Mirrors       — per-agent copies of AGENTS.md (e.g. CLAUDE.md).\n"
    "  Agent dialect — target agent: claude, gemini, opencode, etc.\n"
    "  Applied rules — managed on the Rules [u] screen.\n"
    "  Layout profile — controls where skills, rules, agents, and .mcp.json go.\n"
    "Shortcuts work from the main menu: [l] PROFILES, [u] RULES."
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

    def compose(self) -> ComposeResult:
        yield Static("Config", id="title", markup=False)
        try:
            m = manifest.load(self._project_root)
            has_manifest = True
        except manifest.ManifestNotFoundError:
            m = None
            has_manifest = False

        active_profile = self._active_profile_label()

        yield Vertical(
            Static(f"Project: {self._project_root}", classes="config-paths", markup=False),
            Static(
                "manifest: "
                + str(paths.project_manifest_path(self._project_root))
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
            Static("Template:", classes="config-heading", markup=False),
            Input(value=m.template if m else "default", id="proj-template"),
            Static(
                "Mirrors (per-agent dialect copies of AGENTS.md):",
                classes="config-heading",
                markup=False,
            ),
            *self._mirror_checkboxes(m),
            Static("Other mirror (optional):", classes="config-heading", markup=False),
            Input(value="", placeholder="<name>.md", id="proj-other-mirror"),
            Static(
                "Agent dialect (claude / gemini / opencode / blank):",
                classes="config-heading",
                markup=False,
            ),
            Input(value=(m.agent_dialect or "") if m else "", id="proj-dialect"),
            Static(
                f"Applied rules ({len(m.rules) if m else 0}):",
                classes="config-heading",
                markup=False,
            ),
            Static(
                ", ".join(m.rules) if (m and m.rules) else "(none — manage on the Rules screen)",
                id="proj-rules-display",
                classes="config-paths",
                markup=False,
            ),
            Checkbox("Force overwrite existing files on save", id="proj-force"),
            Button("Save (re-runs init)", id="proj-save", variant="primary"),
            Static(_HELP_TEXT, classes="config-help", markup=True),
            id="project-pane",
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
        return f"{profile.name}  ·  skills:{profile.skills_dir}  rules:{profile.rules_dir}  agents:{profile.agents_dir}  mcp:{profile.mcp_json}"

    def _mirror_checkboxes(self, m: manifest.Manifest | None) -> list[Checkbox]:
        active = set(m.managed_files) if m else set()
        return [
            Checkbox(
                name,
                value=(name in active),
                id=f"proj-mirror-{name.replace('.', '-')}",
            )
            for name in init_mod.KNOWN_MIRRORS
        ]

    def on_mount(self) -> None:
        self._status("edit project settings and save, or press [b] to go back")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "proj-save":
            return
        project = Path(self.query_one("#proj-root", Input).value).expanduser()
        template = self.query_one("#proj-template", Input).value.strip() or "default"
        mirrors: list[str] = [
            name
            for name in init_mod.KNOWN_MIRRORS
            if self.query_one(f"#proj-mirror-{name.replace('.', '-')}", Checkbox).value
        ]
        other = self.query_one("#proj-other-mirror", Input).value.strip()
        if other:
            if not init_mod.is_valid_mirror_name(other):
                self.app.notify(f"other mirror {other!r} invalid", severity="error")
                return
            if other not in mirrors:
                mirrors.append(other)
        dialect = self.query_one("#proj-dialect", Input).value.strip().lower() or None
        force = self.query_one("#proj-force", Checkbox).value
        try:
            result = init_mod.run(
                init_mod.InitOptions(
                    project_root=project,
                    template=template,
                    mirrors=tuple(mirrors),
                    clear_mirrors=True,
                    agent_dialect=dialect,
                    force=force,
                )
            )
        except Exception as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        verb = "Refreshed" if result.re_init else "Initialized"
        self.app.notify(f"{verb} {result.agents_md_path}", title="Project saved")
        for warn in result.region_drift_warnings:
            self.app.notify(warn, severity="warning")
        self._project_root = project.resolve()

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
