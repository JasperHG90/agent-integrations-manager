"""Main menu — landing screen with navigation to other screens.

The TUI is the primary surface: every action you can do via the CLI is
reachable from here without dropping to a shell.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from aim import __version__
from aim.core import init as init_mod
from aim.core import layout_profiles
from aim.core import lock as lock_mod
from aim.core import prune as prune_mod
from aim.core import sync as sync_mod
from aim.tui.modals.confirm import ConfirmModal
from aim.tui.modals.init_modal import InitConfig, InitModal

_ROCKET = (
    "    ████  ",
    "   ██████ ",
    "   ██  ██ ",
    "   ██████ ",
    "  ████████",
    "  ██    ██",
    "     ██   ",
)

_ROCKET_COLOR = "#ffb000"
_META_COLOR = "#7d7869"


def _active_profile_label(project_root: Path) -> str:
    """Return the display label of the active layout profile, or a dash if unresolved.

    Args:
        project_root: Project directory used to resolve the active profile.

    Returns:
        The profile's display name (falling back to its name), or "—" when
        resolution fails.
    """
    try:
        profile = layout_profiles.resolve_active(project_root)
    except Exception:
        return "—"
    return profile.display_name or profile.name


def _render_banner(project_root: Path) -> str:
    """Build the colored rocket banner with aim version, profile, OS, and path.

    Args:
        project_root: Project directory shown in the banner (home is abbreviated to ~).

    Returns:
        A newline-joined, markup-styled banner string for the landing screen.
    """
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    os_name = {"Darwin": "macOS"}.get(platform.system(), platform.system())
    home = str(Path.home())
    path = str(project_root)
    if path.startswith(home):
        path = "~" + path[len(home) :]
    profile_label = _active_profile_label(project_root)

    meta = [
        f"aim v{__version__}",
        f"Profile: {profile_label} · {os_name} · Python {py}",
        path,
    ]
    padded: list[str] = [""] * len(_ROCKET)
    start = (len(_ROCKET) - len(meta)) // 2
    for i, line in enumerate(meta):
        padded[start + i] = line

    lines: list[str] = []
    for art, line in zip(_ROCKET, padded, strict=True):
        art_part = f"[{_ROCKET_COLOR}]{art}[/]"
        if line:
            lines.append(f"{art_part}   [{_META_COLOR}]{line}[/]")
        else:
            lines.append(art_part)
    return "\n".join(lines)


_MENU_ITEMS: list[tuple[str, str, str]] = [
    ("I", "INIT", "create / edit aim.toml declarations"),
    ("K", "LOCK", "resolve aim.toml into aim.lock.toml"),
    ("Y", "SYNC", "apply aim.lock.toml to the project"),
    ("X", "PRUNE", "remove artifacts not listed in aim.lock.toml"),
    ("R", "REPOS", "registered skill source repositories"),
    ("S", "SKILLS", "browse, search, install"),
    ("A", "SUBAGENTS", "browse, search, install sub-agents"),
    ("M", "MCP", "search registry, install MCP servers"),
    ("G", "PLUGINS", "browse, search, install plugins"),
    ("U", "RULES", "global rules library"),
    ("T", "TEMPLATES", "reusable project setups"),
    ("B", "ARCHETYPES", "AGENTS.md base from a repo"),
    ("P", "PROJECT", "installed skills/agents/MCP in the current project"),
    ("C", "CONFIG", "roots, rule-repo overlays, init profiles"),
    ("L", "PROFILES", "layout profiles for agent tooling paths"),
    ("Q", "QUIT", ""),
]


def _render_menu() -> str:
    """Render the navigation menu with aligned columns and generous spacing."""
    width = max(len(name) for _, name, _ in _MENU_ITEMS)
    lines = [f"    {key}    {name:<{width}}    {desc}".rstrip() for key, name, desc in _MENU_ITEMS]
    return "\n" + "\n".join(lines) + "\n"


class MainScreen(Screen[None]):
    """Landing screen with key-bound navigation to every other TUI screen."""

    BINDINGS = [
        ("i", "open_init", "Init project"),
        ("k", "open_lock", "Lock project"),
        ("y", "open_sync", "Sync project"),
        ("x", "open_prune", "Prune project"),
        ("r", "open_repos", "Repos"),
        ("s", "open_skills", "Skills"),
        ("a", "open_agents", "Subagents"),
        ("m", "open_mcp", "MCP servers"),
        ("g", "open_plugins", "Plugins"),
        ("u", "open_rules", "Rules"),
        ("t", "open_templates", "Templates"),
        ("b", "open_archetypes", "Archetypes"),
        ("p", "open_project", "Project"),
        ("c", "open_config", "Config"),
        ("l", "open_layout_profiles", "Layout profiles"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialize the screen, resolving the project root (defaults to cwd).

        Args:
            project_root: Project directory the screen operates on; falls back
                to the current working directory.
        """
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()

    def compose(self) -> ComposeResult:
        """Yield the banner, menu, and hint widgets."""
        yield Vertical(
            Static(_render_banner(self._project_root), id="banner"),
            id="banner-box",
        )
        yield Vertical(
            Static(_render_menu() + "\n", classes="menu-item", markup=False),
            classes="menu",
        )
        yield Static(
            "  I/K/Y/X/R/S/A/M/G/U/T/B/P/C/L  navigate    CTRL+P  palette    Q  quit",
            id="hint",
            markup=False,
        )

    def action_open_repos(self) -> None:
        """Push the registered-repositories screen."""
        from aim.tui.screens.repos_screen import ReposScreen

        self.app.push_screen(ReposScreen())

    def action_open_skills(self) -> None:
        """Push the skills browse/search/install screen."""
        from aim.tui.screens.skills_screen import SkillsScreen

        self.app.push_screen(SkillsScreen())

    def action_open_agents(self) -> None:
        """Push the sub-agents browse/search/install screen."""
        from aim.tui.screens.agents_screen import AgentsScreen

        self.app.push_screen(AgentsScreen())

    def action_open_mcp(self) -> None:
        """Push the MCP servers screen for the current project."""
        from aim.tui.screens.mcp_screen import McpScreen

        self.app.push_screen(McpScreen(project_root=self._project_root))

    def action_open_plugins(self) -> None:
        """Push the plugins browse/search/install screen."""
        from aim.tui.screens.plugin_screen import PluginsScreen

        self.app.push_screen(PluginsScreen())

    def action_open_rules(self) -> None:
        """Push the global rules library screen."""
        from aim.tui.screens.rules_screen import RulesScreen

        self.app.push_screen(RulesScreen())

    def action_open_templates(self) -> None:
        """Push the project templates screen for the current project."""
        from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen

        self.app.push_screen(ProjectTemplatesScreen(project_root=self._project_root))

    def action_open_archetypes(self) -> None:
        """Push the archetypes browse/search/select screen."""
        from aim.tui.screens.archetypes_screen import ArchetypesScreen

        self.app.push_screen(ArchetypesScreen(project_root=self._project_root))

    def action_open_project(self) -> None:
        """Push the installed-artifacts screen for the current project."""
        from aim.tui.screens.project_screen import ProjectScreen

        self.app.push_screen(ProjectScreen(project_root=self._project_root))

    def action_open_config(self) -> None:
        """Push the config screen for the current project."""
        from aim.tui.screens.config_screen import ConfigScreen

        self.app.push_screen(ConfigScreen(project_root=self._project_root))

    def on_screen_resume(self) -> None:
        """Refresh the banner when the screen regains focus."""
        self.query_one("#banner", Static).update(_render_banner(self._project_root))

    def action_open_layout_profiles(self) -> None:
        """Push the layout profiles screen for the current project."""
        from aim.tui.screens.layout_profiles_screen import LayoutProfilesScreen

        self.app.push_screen(LayoutProfilesScreen(project_root=self._project_root))

    def action_open_init(self) -> None:
        """Open the init modal and run init with its returned config."""
        self.app.push_screen(InitModal(project_root=self._project_root), self._run_init)

    def action_open_lock(self) -> None:
        """Run the lock operation on a background worker thread."""
        self.run_worker(self._do_lock_thread, exclusive=True, thread=True)

    def action_open_sync(self) -> None:
        """Open the init modal in sync mode and run sync with its config."""
        self.app.push_screen(
            InitModal(project_root=self._project_root, sync_mode=True), self._run_sync
        )

    def action_open_prune(self) -> None:
        """Plan a prune on a background worker thread."""
        self.run_worker(self._plan_prune_thread, exclusive=True, thread=True)

    def _plan_prune_thread(self) -> None:
        """Compute the prune plan off-thread and prompt for confirmation if needed.

        Notifies on failure or when there is nothing to prune; otherwise pushes a
        confirmation modal listing the items that would be removed.
        """
        try:
            plan_result = prune_mod.plan(prune_mod.PruneOptions(project_root=self._project_root))
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"prune failed: {exc}", severity="error")
            return
        removals = [i for i in plan_result.removed if i.action == "would-remove"]
        if not removals:
            self.app.call_from_thread(self.app.notify, "Nothing to prune.", title="Prune")
            return
        summary = "\n".join(f"{i.kind}: {i.path}" for i in removals)
        for warn in plan_result.warnings:
            self.app.call_from_thread(self.app.notify, warn, severity="warning")
        self.app.call_from_thread(
            self.app.push_screen,
            ConfirmModal(
                f"Remove {len(removals)} item(s)?\n\n{summary}",
                confirm_label="Prune",
            ),
            self._on_prune_confirm,
        )

    def _on_prune_confirm(self, confirmed: bool | None) -> None:
        """Run the prune on a worker thread once the user confirms.

        Args:
            confirmed: Modal result; only ``True`` proceeds with removal.
        """
        if confirmed is not True:
            return
        self.run_worker(self._do_prune_thread, exclusive=True, thread=True)

    def _do_lock_thread(self) -> None:
        """Run the async lock operation off-thread and notify of the outcome."""
        import asyncio

        try:
            result = asyncio.run(
                lock_mod.run(
                    lock_mod.LockOptions(
                        project_root=self._project_root,
                        progress_callback=lambda kind, name, status: self.app.call_from_thread(
                            self.app.notify, f"{kind} {name}: {status}", title="Lock"
                        ),
                    )
                )
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"lock failed: {exc}", severity="error")
            return
        if result.unchanged:
            self.app.call_from_thread(
                self.app.notify, "aim.lock.toml up to date; no changes", title="Lock"
            )
            return
        self.app.call_from_thread(
            self.app.notify,
            f"locked {len(result.locked_skills)} skills, "
            f"{len(result.locked_agents)} agents, "
            f"{len(result.locked_mcp)} mcp servers",
            title="Lock complete",
        )
        for warn in result.warnings:
            self.app.call_from_thread(self.app.notify, warn, severity="warning")
        for err in result.errors:
            self.app.call_from_thread(self.app.notify, err, severity="error")

    def _do_prune_thread(self) -> None:
        """Re-plan and apply the prune off-thread, notifying of each removal."""
        try:
            plan_result = prune_mod.plan(prune_mod.PruneOptions(project_root=self._project_root))
            removals = [i for i in plan_result.removed if i.action == "would-remove"]
            if not removals:
                self.app.call_from_thread(self.app.notify, "Nothing to prune.", title="Prune")
                return
            result = prune_mod.apply(
                prune_mod.PruneOptions(project_root=self._project_root, force=True),
                plan_result,
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"prune failed: {exc}", severity="error")
            return
        for item in result.removed:
            self.app.call_from_thread(self.app.notify, f"{item.action} {item.path}", title="Pruned")
        self.app.call_from_thread(
            self.app.notify,
            f"pruned {len(result.removed)} items, kept {len(result.kept)}",
            title="Prune complete",
        )

    def _run_init(self, config: InitConfig | None) -> None:
        """Run init from the modal's config and notify of the result.

        Args:
            config: Configuration returned by the init modal; ``None`` cancels.
        """
        if config is None:
            return
        try:
            result = init_mod.run(
                init_mod.InitOptions(
                    project_root=config.project_root,
                    layout_profile=config.layout_profile,
                    archetype=config.archetype,
                )
            )
        except Exception as exc:
            self.app.notify(f"init failed: {exc}", severity="error")
            return
        verb = "Refreshed" if result.re_init else "Initialized"
        self.app.notify(f"{verb} {result.declarations_path}", title="Init complete")
        self.app.notify("Run Lock, then Sync, to apply the declarations to disk.")

    def _run_sync(self, config: InitConfig | None) -> None:
        """Stash the sync config and run sync on a worker thread.

        Args:
            config: Configuration returned by the sync modal; ``None`` cancels.
        """
        if config is None:
            return
        self._syncing_config = config
        self.run_worker(self._do_sync_thread, exclusive=True, thread=True)

    def _do_sync_thread(self) -> None:
        """Run the async sync operation off-thread and notify of the outcome."""
        import asyncio

        config = getattr(self, "_syncing_config", None)
        if config is None:
            return
        try:
            result = asyncio.run(
                sync_mod.run(
                    sync_mod.SyncOptions(
                        project_root=config.project_root,
                        force=config.force,
                        sync_agents=config.sync_agents,
                        layout_profile=config.layout_profile,
                        progress_callback=lambda kind, name, status: self.app.call_from_thread(
                            self.app.notify, f"{kind} {name}: {status}", title="Sync"
                        ),
                    )
                )
            )
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"sync failed: {exc}", severity="error")
            return
        self.app.call_from_thread(
            self.app.notify,
            f"synced {len(result.synced_skills)} skills, "
            f"{len(result.synced_agents)} agents, "
            f"{len(result.synced_mcp)} mcp servers",
            title="Sync complete",
        )
        for warn in result.drift_warnings:
            self.app.call_from_thread(self.app.notify, warn, severity="warning")
        for err in result.repo_errors:
            self.app.call_from_thread(self.app.notify, err, severity="error")
