"""Command palette — Ctrl-P opens this overlay. Lists every TUI navigation
action plus every entity (rule, repo, skill) by name. Substring match,
Enter to act.

Uses OptionList rather than DataTable because the latter has a sizing
quirk inside ModalScreen with `height: auto` (the DataTable's `1fr` and
the modal's `auto` can't both resolve, leaving the modal's compositor
visual unset — render fails with "NoneType has no render_strips").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from aim.core import agents as agents_mod
from aim.core import profiles as profiles_mod
from aim.core import repo_rules, repos, skills


@dataclass
class PaletteEntry:
    """Represent a single selectable command-palette row."""

    kind: str  # "action" | "rule" | "repo" | "skill"
    label: str
    handler: Callable[[], None]


class PaletteModal(ModalScreen[PaletteEntry | None]):
    """Modal overlay listing palette entries with substring filtering."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "activate", "Run"),
    ]

    def __init__(self, entries: list[PaletteEntry]) -> None:
        """Initialize the modal with the full set of palette entries.

        Args:
            entries: Every entry available for selection before filtering.
        """
        super().__init__()
        self._all: list[PaletteEntry] = entries
        self._filtered: list[PaletteEntry] = list(entries)

    def compose(self) -> ComposeResult:
        """Build the title, filter input, and option list widgets."""
        yield Vertical(
            Static("Command palette", classes="modal-title", markup=False),
            Input(placeholder="type to filter…", id="palette-input"),
            OptionList(*self._option_widgets(), id="palette-list"),
            id="palette-box",
        )

    def on_mount(self) -> None:
        """Focus the filter input when the modal is mounted."""
        self.query_one("#palette-input", Input).focus()

    def _option_widgets(self) -> list[Option]:
        """Build option widgets for the currently filtered entries.

        Returns:
            One option per filtered entry, labelled with its kind and label.
        """
        return [
            Option(f"[{e.kind}] {e.label}", id=f"opt-{i}") for i, e in enumerate(self._filtered)
        ]

    def _re_render(self) -> None:
        """Replace the option list contents with the current filter result."""
        olist = self.query_one(OptionList)
        olist.clear_options()
        olist.add_options(self._option_widgets())

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter entries by substring as the user types.

        Args:
            event: The input-changed event carrying the current filter text.
        """
        if event.input.id != "palette-input":
            return
        q = event.value.lower().strip()
        if not q:
            self._filtered = list(self._all)
        else:
            self._filtered = [e for e in self._all if q in e.label.lower() or q in e.kind]
        self._re_render()

    def action_activate(self) -> None:
        """Dismiss the modal with the highlighted entry, or the first if none."""
        olist = self.query_one(OptionList)
        idx = olist.highlighted
        if idx is None and self._filtered:
            idx = 0
        if idx is None or idx >= len(self._filtered):
            return
        self.dismiss(self._filtered[idx])

    def action_cancel(self) -> None:
        """Dismiss the modal without selecting an entry."""
        self.dismiss(None)


def build_entries(app) -> list[PaletteEntry]:  # type: ignore[no-untyped-def]
    """Construct the palette entries for the current global state.

    Args:
        app: The running TUI app, used to push screens and read project state.

    Returns:
        Navigation action entries followed by one entry per known template,
        repo, skill, agent, and rule.
    """
    from aim.tui.screens.agents_screen import AgentsScreen
    from aim.tui.screens.config_screen import ConfigScreen
    from aim.tui.screens.layout_profiles_screen import LayoutProfilesScreen
    from aim.tui.screens.mcp_screen import McpScreen
    from aim.tui.screens.plugin_screen import PluginsScreen
    from aim.tui.screens.project_screen import ProjectScreen
    from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen
    from aim.tui.screens.repos_screen import ReposScreen
    from aim.tui.screens.rules_screen import RulesScreen
    from aim.tui.screens.skills_screen import SkillsScreen

    project_root = getattr(app, "_project_root", None)
    entries: list[PaletteEntry] = [
        PaletteEntry("action", "Open Repos", lambda: app.push_screen(ReposScreen())),
        PaletteEntry("action", "Open Skills", lambda: app.push_screen(SkillsScreen())),
        PaletteEntry("action", "Open Agents", lambda: app.push_screen(AgentsScreen())),
        PaletteEntry(
            "action",
            "Open MCP servers",
            lambda: app.push_screen(McpScreen(project_root=project_root)),
        ),
        PaletteEntry("action", "Open Plugins", lambda: app.push_screen(PluginsScreen())),
        PaletteEntry("action", "Open Rules", lambda: app.push_screen(RulesScreen())),
        PaletteEntry(
            "action", "Open Project templates", lambda: app.push_screen(ProjectTemplatesScreen())
        ),
        PaletteEntry("action", "Open Project", lambda: app.push_screen(ProjectScreen())),
        PaletteEntry("action", "Open Profiles", lambda: app.push_screen(LayoutProfilesScreen())),
        PaletteEntry("action", "Open Config", lambda: app.push_screen(ConfigScreen())),
        PaletteEntry("action", "Quit", lambda: app.exit()),
    ]
    for template in profiles_mod.list_profiles():
        entries.append(
            PaletteEntry(
                "template",
                template.name,
                lambda: app.push_screen(ProjectTemplatesScreen()),
            )
        )
    for repo in repos.list_repos():
        entries.append(
            PaletteEntry(
                "repo",
                repo.alias,
                lambda: app.push_screen(ReposScreen()),
            )
        )
    for skill in skills.list_skills():
        entries.append(
            PaletteEntry(
                "skill",
                skill.qualified_name,
                lambda: app.push_screen(SkillsScreen()),
            )
        )
    for agent in agents_mod.list_agents():
        entries.append(
            PaletteEntry(
                "agent",
                agent.qualified_name,
                lambda: app.push_screen(AgentsScreen()),
            )
        )
    for rule in repo_rules.list_rules():
        entries.append(
            PaletteEntry(
                "rule",
                rule.qualified_name,
                lambda: app.push_screen(RulesScreen()),
            )
        )
    return entries
