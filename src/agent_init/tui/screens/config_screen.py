"""Config screen — two tabs:

- **GLOBAL**: machine-wide resources (roots used by `doctor`, registered
  rule-repo overlays, saved profiles). All paths shown explicitly so users
  can find their state on disk.

- **PROJECT**: the *current* project's manifest, editable. Template, mirrors,
  agent_dialect, applied rules. Save re-runs `init` against the project.

Skill installs and rule bodies live on their own screens (Skills, Rules) —
this pane is for settings, not entity authoring.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Static,
    TabbedContent,
    TabPane,
)

from agent_init.core import (
    init as init_mod,
)
from agent_init.core import (
    manifest,
    paths,
    profiles,
    roots,
    rule_repos,
)
from agent_init.tui.modals.confirm import ConfirmModal


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
        yield Static(
            "Config",
            id="title",
            markup=False,
        )
        with TabbedContent(initial="global"):
            with TabPane("GLOBAL", id="global"):
                yield from self._compose_global()
            with TabPane("PROJECT", id="project"):
                yield from self._compose_project()
        yield Static("", id="status", markup=False)
        yield Static(
            "[Tab/Shift+Tab] switch tabs  [b] Back  [q] Quit",
            id="hint",
            markup=False,
        )

    # ---------- GLOBAL tab ----------

    def _compose_global(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"data_dir:   {paths.user_data_dir()}\n"
                f"cache_dir:  {paths.user_cache_dir()}\n"
                f"config_dir: {paths.user_config_dir()}",
                classes="config-paths",
                markup=False,
            ),
            Static("Project roots (audited by `doctor`):", classes="config-heading", markup=False),
            DataTable(id="roots-table", cursor_type="row", show_header=False),
            Static("Registered rule-repo overlays:", classes="config-heading", markup=False),
            DataTable(id="rule-repos-table", cursor_type="row"),
            Static("Saved init profiles:", classes="config-heading", markup=False),
            DataTable(id="profiles-table", cursor_type="row"),
            Static(
                "Delete: focus a row and press X. Add: use CLI for now "
                "(`root add`, `rule-repo add`, `profile save`).",
                classes="config-hint",
                markup=False,
            ),
            id="global-pane",
        )

    # ---------- PROJECT tab ----------

    def _compose_project(self) -> ComposeResult:
        try:
            m = manifest.load(self._project_root)
            has_manifest = True
        except manifest.ManifestNotFoundError:
            m = None
            has_manifest = False

        yield Vertical(
            Static(f"Project: {self._project_root}", classes="config-paths", markup=False),
            Static(
                "manifest: "
                + str(paths.project_manifest_path(self._project_root))
                + ("" if has_manifest else "  (not initialized — fields below will create one)"),
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
            id="project-pane",
        )

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

    # ---------- lifecycle ----------

    def on_mount(self) -> None:
        self.query_one("#roots-table", DataTable).add_columns("path")
        self.query_one("#rule-repos-table", DataTable).add_columns("alias", "url", "last sha")
        self.query_one("#profiles-table", DataTable).add_columns(
            "name", "template", "mirrors", "rules", "skills"
        )
        self._populate_global()

    def on_screen_resume(self) -> None:
        self._populate_global()

    def _populate_global(self) -> None:
        roots_t = self.query_one("#roots-table", DataTable)
        roots_t.clear()
        for r in roots.list_roots():
            roots_t.add_row(str(r), key=str(r))

        rr_t = self.query_one("#rule-repos-table", DataTable)
        rr_t.clear()
        for rr in rule_repos.list_repos():
            rr_t.add_row(rr.alias, rr.url, (rr.last_sha or "?")[:12], key=rr.alias)

        prof_t = self.query_one("#profiles-table", DataTable)
        prof_t.clear()
        for p in profiles.list_profiles():
            prof_t.add_row(
                p.name,
                p.template,
                ",".join(p.mirrors) if p.mirrors else "-",
                str(len(p.rules)),
                str(len(p.skills)),
                key=p.name,
            )
        self._status(
            f"{roots_t.row_count} root(s) · {rr_t.row_count} rule-repo(s) · {prof_t.row_count} profile(s)"
        )

    # ---------- GLOBAL: delete on X ----------

    def key_x(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.row_count == 0:
            return
        row_key, _ = focused.coordinate_to_cell_key(focused.cursor_coordinate)
        if row_key is None or row_key.value is None:
            return
        key = str(row_key.value)
        table_id = focused.id

        def _on_confirm(yes: bool | None) -> None:
            if yes is not True:
                return
            try:
                if table_id == "roots-table":
                    roots.remove_root(Path(key))
                elif table_id == "rule-repos-table":
                    rule_repos.remove(key)
                elif table_id == "profiles-table":
                    profiles.delete(key)
            except Exception as exc:
                self.app.notify(f"delete failed: {exc}", severity="error")
                return
            self.app.notify(f"deleted {key}")
            self._populate_global()

        self.app.push_screen(ConfirmModal(f"Delete {key!r}?"), _on_confirm)

    # ---------- PROJECT: save ----------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "proj-save":
            return
        proj = Path(self.query_one("#proj-root", Input).value).expanduser()
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
                    project_root=proj,
                    template=template,
                    mirrors=tuple(mirrors),
                    clear_mirrors=True,  # explicit set — replace prior mirrors
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
        self._project_root = proj.resolve()

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)
