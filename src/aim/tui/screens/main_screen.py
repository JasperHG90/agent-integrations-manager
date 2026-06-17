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
    try:
        profile = layout_profiles.resolve_active(project_root)
    except Exception:
        return "—"
    return profile.display_name or profile.name


def _render_banner(project_root: Path) -> str:
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


class MainScreen(Screen[None]):
    BINDINGS = [
        ("i", "open_init", "Init project"),
        ("k", "open_lock", "Lock project"),
        ("y", "open_sync", "Sync project"),
        ("x", "open_prune", "Prune project"),
        ("r", "open_repos", "Repos"),
        ("s", "open_skills", "Skills"),
        ("a", "open_agents", "Subagents"),
        ("m", "open_mcp", "MCP servers"),
        ("u", "open_rules", "Rules"),
        ("t", "open_templates", "Templates"),
        ("p", "open_project", "Project"),
        ("c", "open_config", "Config"),
        ("l", "open_layout_profiles", "Layout profiles"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self._project_root = (project_root or Path.cwd()).resolve()

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(_render_banner(self._project_root), id="banner"),
            id="banner-box",
        )
        yield Vertical(
            Static(
                "\n"
                "    I   INIT      create / edit aim.toml declarations\n"
                "    K   LOCK      resolve aim.toml into aim.lock.toml\n"
                "    Y   SYNC      apply aim.lock.toml to the project\n"
                "    X   PRUNE     remove artifacts not listed in aim.lock.toml\n"
                "    R   REPOS     registered skill source repositories\n"
                "    S   SKILLS    browse, search, install\n"
                "    A   SUBAGENTS browse, search, install sub-agents\n"
                "    M   MCP       search registry, install MCP servers\n"
                "    U   RULES     global rules library\n"
                "    T   TEMPLATES reusable project setups\n"
                "    P   PROJECT   installed skills/agents/MCP in the current project\n"
                "    C   CONFIG    roots, rule-repo overlays, init profiles\n"
                "    L   PROFILES  layout profiles for agent tooling paths\n"
                "    Q   QUIT\n"
                "\n",
                classes="menu-item",
                markup=False,
            ),
            classes="menu",
        )
        yield Static(
            "  I/K/Y/X/R/S/A/M/U/T/P/C/L  navigate    CTRL+P  palette    Q  quit",
            id="hint",
            markup=False,
        )

    def action_open_repos(self) -> None:
        from aim.tui.screens.repos_screen import ReposScreen

        self.app.push_screen(ReposScreen())

    def action_open_skills(self) -> None:
        from aim.tui.screens.skills_screen import SkillsScreen

        self.app.push_screen(SkillsScreen())

    def action_open_agents(self) -> None:
        from aim.tui.screens.agents_screen import AgentsScreen

        self.app.push_screen(AgentsScreen())

    def action_open_mcp(self) -> None:
        from aim.tui.screens.mcp_screen import McpScreen

        self.app.push_screen(McpScreen(project_root=self._project_root))

    def action_open_rules(self) -> None:
        from aim.tui.screens.rules_screen import RulesScreen

        self.app.push_screen(RulesScreen())

    def action_open_templates(self) -> None:
        from aim.tui.screens.project_templates_screen import ProjectTemplatesScreen

        self.app.push_screen(ProjectTemplatesScreen(project_root=self._project_root))

    def action_open_project(self) -> None:
        from aim.tui.screens.project_screen import ProjectScreen

        self.app.push_screen(ProjectScreen(project_root=self._project_root))

    def action_open_config(self) -> None:
        from aim.tui.screens.config_screen import ConfigScreen

        self.app.push_screen(ConfigScreen(project_root=self._project_root))

    def action_open_layout_profiles(self) -> None:
        from aim.tui.screens.layout_profiles_screen import LayoutProfilesScreen

        self.app.push_screen(LayoutProfilesScreen(project_root=self._project_root))

    def action_open_init(self) -> None:
        self.app.push_screen(InitModal(project_root=self._project_root), self._run_init)

    def action_open_lock(self) -> None:
        self.run_worker(self._do_lock_thread, exclusive=True, thread=True)

    def action_open_sync(self) -> None:
        self.app.push_screen(InitModal(project_root=self._project_root, sync_mode=True), self._run_sync)

    def action_open_prune(self) -> None:
        self.run_worker(self._do_prune_thread, exclusive=True, thread=True)

    def _do_lock_thread(self) -> None:
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
        try:
            result = prune_mod.run(prune_mod.PruneOptions(project_root=self._project_root, dry_run=False))
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
        if config is None:
            return
        try:
            result = init_mod.run(
                init_mod.InitOptions(
                    project_root=config.project_root,
                    layout_profile=config.layout_profile,
                )
            )
        except Exception as exc:
            self.app.notify(f"init failed: {exc}", severity="error")
            return
        verb = "Refreshed" if result.re_init else "Initialized"
        self.app.notify(f"{verb} {result.declarations_path}", title="Init complete")
        self.app.notify("Run Lock, then Sync, to apply the declarations to disk.")

    def _run_sync(self, config: InitConfig | None) -> None:
        if config is None:
            return
        self._syncing_config = config
        self.run_worker(self._do_sync_thread, exclusive=True, thread=True)

    def _do_sync_thread(self) -> None:
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
