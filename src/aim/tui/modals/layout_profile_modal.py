"""Modal: add or edit a layout profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from aim.core import layout_profiles


def _project_target_lines(project_root: Path) -> list[str]:
    """Describe each plugin target active for the project, one line per target.

    Targets — not the layout profile — own where a client's plugins vendor
    (`vendor_into`), so the profile editor surfaces them read-only to complete
    the layout picture.
    """
    from aim.core import plugin_kinds

    lines: list[str] = []
    for name, kind in sorted(plugin_kinds.load_kinds(project_root).items()):
        spec = getattr(kind, "spec", None)
        if spec is None:
            continue  # built-in kinds (claude) have fixed, code-owned paths
        lines.append(f"{name}: plugins → {spec.registration.vendor_into}")
    return lines


@dataclass(frozen=True)
class LayoutProfileResult:
    """Outcome of the modal: the edited profile plus its prior name, if any."""

    profile: layout_profiles.LayoutProfile
    original_name: str | None = None


class LayoutProfileModal(ModalScreen[LayoutProfileResult | None]):
    """Modal screen for adding or editing a layout profile."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        project_root: Path,
        *,
        profile: layout_profiles.LayoutProfile | None = None,
    ) -> None:
        """Initialize the modal.

        Args:
            project_root: Repository root the profile applies to.
            profile: Existing profile to edit, or None to add a new one.
        """
        super().__init__()
        self._project_root = project_root
        self._original = profile
        self._is_builtin = profile is not None and profile.name in (
            layout_profiles.BUILTIN_CLAUDE.name,
            layout_profiles.BUILTIN_GEMINI.name,
        )

    def compose(self) -> ComposeResult:
        """Build the modal's form widgets."""
        p = self._original
        yield Vertical(
            Static(
                "Edit layout profile" if p else "Add layout profile",
                classes="modal-title",
                markup=False,
            ),
            VerticalScroll(
                Static("Name:", markup=False),
                Input(value=(p.name if p else ""), id="name"),
                Static("Display name:", markup=False),
                Input(value=(p.display_name or "" if p else ""), id="display-name"),
                Static("Description:", markup=False),
                Input(value=(p.description or "" if p else ""), id="description"),
                Static("Scope:", markup=False),
                RadioSet(
                    RadioButton("project", id="scope-project", value=True),
                    RadioButton("global", id="scope-global"),
                    id="scope",
                ),
                Static(
                    "project: saved only in this repo. global: cached in DB for all projects, "
                    "with a read-only copy here.",
                    id="scope-help",
                    markup=False,
                ),
                Static("Rules mode:", markup=False),
                RadioSet(
                    RadioButton("files", id="rules-mode-files", value=True),
                    RadioButton("inline", id="rules-mode-inline"),
                    id="rules-mode",
                ),
                Static(
                    "files: write rules to rules_dir/<name>.md. "
                    "inline: render rule bodies into the AGENTS.md rules region.",
                    id="rules-mode-help",
                    markup=False,
                ),
                Static("Rules directory:", markup=False),
                Input(
                    value=(p.rules_dir if p else ".aim/rules"),
                    id="rules-dir",
                ),
                Static("Skills directory:", markup=False),
                Input(
                    value=(p.skills_dir if p else ".claude/skills"),
                    id="skills-dir",
                ),
                Static("Subagents file:", markup=False),
                Input(value=(p.agents_md if p else "AGENTS.md"), id="agents-md"),
                Static("Symlinks (comma-separated):", markup=False),
                Input(
                    value=(",".join(p.symlinks) if p else ""),
                    placeholder="CLAUDE.md, GEMINI.md",
                    id="symlinks",
                ),
                Static("Plugin targets (read-only):", markup=False),
                Static(
                    "\n".join(_project_target_lines(self._project_root))
                    or "none — install targets on the Targets tab",
                    id="targets-info",
                    markup=False,
                ),
                Static(
                    "Where plugins land is owned by each target's TOML (.aim/targets/), "
                    "not the profile. View or install targets on the Targets tab.",
                    id="targets-help",
                    markup=False,
                ),
                Static("", id="error", markup=False, classes="modal-error"),
                classes="modal-scroll",
            ),
            Horizontal(
                Button("Save", id="save", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the name field and preselect radios matching the edited profile."""
        self.query_one("#name", Input).focus()
        if self._original:
            scope = self._original.scope
            project_btn = self.query_one("#scope-project", RadioButton)
            global_btn = self.query_one("#scope-global", RadioButton)
            if scope == layout_profiles.LayoutProfileScope.PROJECT:
                project_btn.value = True
            else:
                global_btn.value = True
            files_btn = self.query_one("#rules-mode-files", RadioButton)
            inline_btn = self.query_one("#rules-mode-inline", RadioButton)
            if self._original.rules_mode == "inline":
                inline_btn.value = True
            else:
                files_btn.value = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Submit on Save; dismiss without a result otherwise."""
        if event.button.id == "save":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        """Dismiss the modal without a result."""
        self.dismiss(None)

    def _error(self, msg: str, focus_id: str) -> None:
        """Show an inline error, focus the offending field, and notify the user.

        Args:
            msg: Error message to display.
            focus_id: Widget id to focus so the user can correct the input.
        """
        self.query_one("#error", Static).update(msg)
        widget = self.query_one(f"#{focus_id}")
        if hasattr(widget, "focus"):
            widget.focus()
        self.app.notify(msg, severity="error", title="Layout profile")

    def _submit(self) -> None:
        """Validate the form, build a profile, and dismiss with the result.

        Shows an inline error and returns early when the name is missing, the
        profile fails validation, or an existing built-in profile would be
        overwritten under its own name.
        """
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._error("name is required", "name")
            return

        display_name = self.query_one("#display-name", Input).value.strip() or None
        description = self.query_one("#description", Input).value.strip() or None
        scope = self._read_scope()
        rules_mode = self._read_rules_mode()
        rules_dir = self.query_one("#rules-dir", Input).value.strip()
        skills_dir = self.query_one("#skills-dir", Input).value.strip()
        agents_md = self.query_one("#agents-md", Input).value.strip()
        symlinks_raw = self.query_one("#symlinks", Input).value.strip()
        symlinks = [s.strip() for s in symlinks_raw.split(",") if s.strip()]

        try:
            profile = layout_profiles.LayoutProfile(
                name=name,
                display_name=display_name,
                description=description,
                scope=scope,
                rules_mode=rules_mode,
                rules_dir=rules_dir,
                skills_dir=skills_dir,
                agents_md=agents_md,
                symlinks=symlinks,
            )
        except Exception as exc:
            self._error(f"invalid profile: {exc}", "name")
            return

        if self._is_builtin and self._original is not None and name == self._original.name:
            self._error(
                "built-in profiles cannot be overwritten; choose a different name",
                "name",
            )
            return

        original_name = self._original.name if self._original else None
        self.dismiss(LayoutProfileResult(profile=profile, original_name=original_name))

    def _read_scope(self) -> layout_profiles.LayoutProfileScope:
        """Return the selected scope, defaulting to PROJECT."""
        rs = self.query_one("#scope", RadioSet)
        pressed = rs.pressed_button
        if pressed is not None and pressed.id == "scope-global":
            return layout_profiles.LayoutProfileScope.GLOBAL
        return layout_profiles.LayoutProfileScope.PROJECT

    def _read_rules_mode(self) -> Literal["files", "inline"]:
        """Return the selected rules mode, defaulting to "files"."""
        rs = self.query_one("#rules-mode", RadioSet)
        pressed = rs.pressed_button
        if pressed is not None and pressed.id == "rules-mode-inline":
            return "inline"
        return "files"
