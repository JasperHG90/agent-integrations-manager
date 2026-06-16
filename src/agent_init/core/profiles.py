"""Project profiles — named bundles of init settings.

A profile snapshots a project's `(template, mirrors, rules, skills, agents,
mcp_servers, agent_dialect, layout_profile)` so you can stamp out new projects
from it. Stored as JSON under `user_config_dir/profiles/<name>.json`. Skills,
agents and MCP servers reference upstream by qualified_name/registry_name +
pin/track, not by frozen bytes — so applying a profile always picks up the
latest version unless pinned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_init.core import agent_install as agent_install_mod
from agent_init.core import init as init_mod
from agent_init.core import install as install_mod
from agent_init.core import mcp_install as mcp_install_mod
from agent_init.core import mcp_registry as mcp_registry_mod
from agent_init.core import paths

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProfileNameError(ValueError):
    pass


class ProfileNotFoundError(KeyError):
    pass


class ProfileSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    pin: str | None = None
    track: str | None = None


class ProfileAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    pin: str | None = None
    track: str | None = None


class ProfileMcpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_name: str
    alias: str
    transport: str | None = None
    overrides: dict[str, object] | None = None


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    template: str = "default"
    layout_profile: str | None = None
    mirrors: list[str] = Field(default_factory=list)
    symlinks: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    skills: list[ProfileSkill] = Field(default_factory=list)
    agents: list[ProfileAgent] = Field(default_factory=list)
    mcp_servers: list[ProfileMcpServer] = Field(default_factory=list)
    agent_dialect: str | None = None


def _profile_path(name: str) -> Path:
    return paths.user_config_dir() / "profiles" / f"{name}.json"


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ProfileNameError(
            f"profile name {name!r} invalid: must be lowercase alphanumeric, _, or -"
        )


def save(profile: Profile) -> Path:
    _validate_name(profile.name)
    paths.ensure_global_dirs()
    path = _profile_path(profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2) + "\n")
    return path


def load(name: str) -> Profile:
    path = _profile_path(name)
    if not path.exists():
        raise ProfileNotFoundError(name)
    return Profile.model_validate(json.loads(path.read_text()))


def list_profiles() -> list[Profile]:
    dir_ = paths.user_config_dir() / "profiles"
    if not dir_.exists():
        return []
    out: list[Profile] = []
    for path in sorted(dir_.glob("*.json")):
        try:
            out.append(Profile.model_validate(json.loads(path.read_text())))
        except Exception:  # pragma: no cover — corrupt file
            continue
    return out


def delete(name: str) -> bool:
    path = _profile_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def from_project(name: str, project_root: Path) -> Profile:
    """Build a profile by inspecting a project's manifest and disk state."""
    from agent_init.core import manifest

    m = manifest.load(project_root)
    managed = [
        f for f in m.managed_files if f.lower() != "agents.md"
    ]
    mirrors: list[str] = []
    symlinks: list[str] = []
    for filename in managed:
        path = project_root / filename
        if path.is_symlink():
            symlinks.append(filename)
        else:
            mirrors.append(filename)
    return Profile(
        name=name,
        template=m.template,
        layout_profile=m.layout_profile,
        mirrors=mirrors,
        symlinks=symlinks,
        rules=list(m.rules),
        skills=[
            ProfileSkill(
                qualified_name=s.qualified_name, pin=s.pin, track=s.track
            )
            for s in m.skills
        ],
        agents=[
            ProfileAgent(
                qualified_name=a.qualified_name, pin=a.pin, track=a.track
            )
            for a in m.agents
        ],
        mcp_servers=[
            ProfileMcpServer(
                registry_name=ms.registry_name,
                alias=ms.alias,
                transport=ms.entry.type if ms.entry else None,
                overrides=None,
            )
            for ms in m.mcp_servers
        ],
        agent_dialect=m.agent_dialect,
    )


@dataclass
class ProfileApplyResult:
    project_root: Path
    init_result: object
    installed_skills: list[str] = field(default_factory=list)
    skipped_skills: list[str] = field(default_factory=list)
    installed_agents: list[str] = field(default_factory=list)
    skipped_agents: list[str] = field(default_factory=list)
    installed_mcp: list[str] = field(default_factory=list)
    skipped_mcp: list[str] = field(default_factory=list)


def apply(name: str, project_root: Path) -> ProfileApplyResult:
    """Apply a profile to a project: run init, then install skills/agents/MCP."""
    profile = load(name)
    init_result = init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            template=profile.template,
            layout_profile=profile.layout_profile,
            mirrors=tuple(profile.mirrors),
            symlinks=tuple(profile.symlinks),
            extra_rules=list(profile.rules),
            agent_dialect=profile.agent_dialect,
        )
    )
    installed_skills: list[str] = []
    skipped_skills: list[str] = []
    for ps in profile.skills:
        try:
            install_mod.install(
                project_root, ps.qualified_name, track=ps.track, pin=ps.pin
            )
            installed_skills.append(ps.qualified_name)
        except install_mod.SkillNotIndexedError:
            skipped_skills.append(ps.qualified_name)

    installed_agents: list[str] = []
    skipped_agents: list[str] = []
    for pa in profile.agents:
        try:
            agent_install_mod.install(
                project_root, pa.qualified_name, track=pa.track, pin=pa.pin
            )
            installed_agents.append(pa.qualified_name)
        except agent_install_mod.AgentNotIndexedError:
            skipped_agents.append(pa.qualified_name)

    installed_mcp: list[str] = []
    skipped_mcp: list[str] = []
    for pm in profile.mcp_servers:
        try:
            mcp_install_mod.install(
                project_root,
                pm.registry_name,
                alias=pm.alias,
                preferred_transport=pm.transport,
                overrides=pm.overrides,
                force=False,
            )
            installed_mcp.append(pm.alias)
        except (
            mcp_registry_mod.McpRegistryError,
            mcp_registry_mod.McpMappingError,
            mcp_install_mod.McpAliasConflictError,
            mcp_install_mod.McpAliasInvalidError,
        ):
            skipped_mcp.append(pm.alias)

    return ProfileApplyResult(
        project_root=project_root,
        init_result=init_result,
        installed_skills=installed_skills,
        skipped_skills=skipped_skills,
        installed_agents=installed_agents,
        skipped_agents=skipped_agents,
        installed_mcp=installed_mcp,
        skipped_mcp=skipped_mcp,
    )
