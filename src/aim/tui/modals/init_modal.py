"""Modal: configure `init` for a project. Pick project root, template, and
which symlink files to write."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from aim.core import archetypes, layout_profiles
from aim.core.init import BUILTIN_INSTRUCTIONS
from aim.tui.widgets import ToggleRow


@dataclass(frozen=True)
class InitConfig:
    """Hold the user's choices for initializing or syncing a project."""

    project_root: Path
    layout_profile: str | None
    sync_agents: bool
    force: bool
    # Instruction archetype qualified name, BUILTIN_INSTRUCTIONS for the built-in
    # template, or None to leave the current selection unchanged.
    instruction_archetype: str | None = None


class InitModal(ModalScreen[InitConfig | None]):
    """Modal screen for configuring an `init` (or sync) run on a project."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Initialize", priority=True),
    ]

    def __init__(self, *, project_root: Path | None = None, sync_mode: bool = False) -> None:
        """Initialize the modal with an optional project root and sync mode.

        Args:
            project_root: Project directory to pre-fill; defaults to the current directory.
            sync_mode: Render the modal as a "Sync project" flow instead of "Initialize".
        """
        super().__init__()
        self._initial_project = (project_root or Path.cwd()).resolve()
        self._profile_options: list[tuple[str, str]] = []
        self._sync_mode = sync_mode

    @staticmethod
    def _build_profile_options(project_root: Path) -> list[tuple[str, str]]:
        """Build (label, value) options for the layout-profile select.

        Args:
            project_root: Project directory whose available layout profiles are listed.

        Returns:
            A list of (display name, profile name) tuples for the Select widget.
        """
        profiles = layout_profiles.list_profiles(project_root)
        return [(p.display_name or p.name, p.name) for p in profiles]

    @staticmethod
    def _build_instruction_options() -> list[tuple[str, str]]:
        """Build (label, value) options for the instructions select.

        Returns:
            The built-in template followed by every discovered archetype.
        """
        options = [("built-in template", BUILTIN_INSTRUCTIONS)]
        options.extend(
            (row.qualified_name, row.qualified_name) for row in archetypes.list_archetypes()
        )
        return options

    def compose(self) -> ComposeResult:
        """Build the modal's widget tree."""
        self._profile_options = self._build_profile_options(self._initial_project)
        self._instruction_options = self._build_instruction_options()

        common_widgets = [
            Static("Project root:", markup=False),
            Input(value=str(self._initial_project), id="project-root"),
            Static("Profile:", markup=False),
            Select(self._profile_options, id="layout-profile", allow_blank=True),
            Static("Profile determines symlinks and where rules are written.", markup=False),
            Static("Instructions (AGENTS.md base):", markup=False),
            Select(self._instruction_options, id="instructions", allow_blank=True),
            Static("Built-in template, or a repo archetype as the AGENTS.md base.", markup=False),
            ToggleRow("Sync agent files (AGENTS.md / symlinks)", value=True, id="sync-agents"),
            ToggleRow("Force overwrite if files exist", id="force"),
        ]

        title = "Sync project" if self._sync_mode else "Initialize project"
        scroll_children = [Static(title, classes="modal-title", markup=False)]
        scroll_children.extend(common_widgets)
        scroll_children.append(Static("", id="error", markup=False, classes="modal-error"))

        yield Vertical(
            VerticalScroll(*scroll_children, classes="modal-scroll"),
            Horizontal(
                Button("Initialize" if not self._sync_mode else "Sync", id="go", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="modal-buttons",
            ),
            classes="modal",
        )

    def on_mount(self) -> None:
        """Focus the project-root input when the modal mounts."""
        self.query_one("#project-root", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Submit on the primary button, otherwise dismiss the modal."""
        if event.button.id == "go":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit when the project-root input is confirmed with enter."""
        if event.input.id == "project-root":
            self._submit()

    def action_submit(self) -> None:
        """Submit the modal."""
        self._submit()

    def action_cancel(self) -> None:
        """Dismiss the modal without a result."""
        self.dismiss(None)

    def on_key(self, event) -> None:
        """Cancel on escape, stopping propagation so the binding does not double-fire."""
        if event.key == "escape":
            event.stop()
            self.action_cancel()

    def _error(self, msg: str, focus_id: str) -> None:
        """Show a validation error and focus the offending field.

        Args:
            msg: Error message to display in the modal and a notification.
            focus_id: Widget id to focus so the user can correct the input.
        """
        self.query_one("#error", Static).update(msg)
        self.query_one(f"#{focus_id}", Input).focus()
        self.app.notify(msg, severity="error", title="Init")

    def _submit(self) -> None:
        """Validate inputs and dismiss the modal with an InitConfig, or show an error."""
        project_root_str = self.query_one("#project-root", Input).value.strip()
        if not project_root_str:
            self._error("project root is required", "project-root")
            return

        sync_agents = self.query_one("#sync-agents", ToggleRow).value
        force = self.query_one("#force", ToggleRow).value
        profile_value = self.query_one("#layout-profile", Select).value
        layout_profile: str | None = None
        if isinstance(profile_value, tuple):
            layout_profile = profile_value[1]
        elif profile_value is not None and profile_value not in (Select.BLANK, Select.NULL):
            layout_profile = str(profile_value)

        instructions_value = self.query_one("#instructions", Select).value
        instruction_archetype: str | None = None
        if isinstance(instructions_value, tuple):
            instruction_archetype = instructions_value[1]
        elif instructions_value is not None and instructions_value not in (
            Select.BLANK,
            Select.NULL,
        ):
            instruction_archetype = str(instructions_value)

        self.dismiss(
            InitConfig(
                project_root=Path(project_root_str).expanduser(),
                layout_profile=layout_profile,
                sync_agents=sync_agents,
                force=force,
                instruction_archetype=instruction_archetype,
            )
        )
