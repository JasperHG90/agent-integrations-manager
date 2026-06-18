"""Project profiles — named bundles of init settings.

A profile snapshots a project's `(instruction_template, symlinks, rules, skills,
agents, mcp_servers, layout_profile)` so you can stamp out new
projects from it. Stored as JSON under `user_config_dir/profiles/<name>.json`.
Skills, agents and MCP servers reference upstream by qualified_name/registry_name
+ pin/track, not by frozen bytes — so applying a profile always picks up the
latest version unless pinned.

Profiles can also be imported/exported as TOML for easy sharing and editing.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aim.core import agent_install as agent_install_mod
from aim.core import init as init_mod
from aim.core import install as install_mod
from aim.core import lock as lock_mod
from aim.core import mcp_install as mcp_install_mod
from aim.core import mcp_registry as mcp_registry_mod
from aim.core import paths
from aim.core import rule_install as rule_install_mod
from aim.core import sync as sync_mod
from aim.core.validation import is_valid_mirror_name, is_valid_rule_name

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProfileNameError(ValueError):
    pass


class ProfileNotFoundError(KeyError):
    pass


class ProfileTomlError(ValueError):
    """Invalid TOML or content for a project profile."""

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
    instruction_template: str = "default"
    layout_profile: str | None = None
    symlinks: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    skills: list[ProfileSkill] = Field(default_factory=list)
    agents: list[ProfileAgent] = Field(default_factory=list)
    mcp_servers: list[ProfileMcpServer] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name_field(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ProfileNameError(
                f"profile name {value!r} invalid: must be lowercase alphanumeric, _, or -"
            )
        return value

    @field_validator("symlinks")
    @classmethod
    def _validate_symlink_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not is_valid_mirror_name(value):
                raise ValueError(f"filename {value!r} invalid")
        return values

    @field_validator("rules")
    @classmethod
    def _validate_rule_names(cls, values: list[str]) -> list[str]:
        # Rules are repo-sourced; each entry is a qualified name "<alias>/<rule>".
        for value in values:
            _alias, _, name = value.partition("/")
            if not name or not is_valid_rule_name(name):
                raise ValueError(f"rule qualified name {value!r} invalid (expected <alias>/<rule>)")
        return values


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
    _validate_name(name)
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
        if not _NAME_RE.fullmatch(path.stem):
            continue
        try:
            out.append(Profile.model_validate(json.loads(path.read_text())))
        except Exception:  # pragma: no cover — corrupt file
            continue
    return out


def delete(name: str) -> bool:
    _validate_name(name)
    path = _profile_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


_TOML_KEY_MAP = {
    "skill": "skills",
    "subagent": "agents",
    "mcp_server": "mcp_servers",
}


def parse_toml(text: str, *, source: str | None = None) -> Profile:
    """Parse a project profile from a TOML string.

    TOML uses singular array-of-table headers per item:
        [[skill]], [[subagent]], [[mcp_server]]
    These are mapped to the plural field names on the Profile model.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileTomlError(f"invalid TOML in {source or 'profile'}: {exc}") from exc
    for singular, plural in _TOML_KEY_MAP.items():
        if singular in raw:
            raw[plural] = raw.pop(singular)
    try:
        return Profile.model_validate(raw)
    except (ProfileNameError, ValueError) as exc:
        raise ProfileTomlError(str(exc)) from exc


def render_toml(profile: Profile) -> str:
    """Serialize a project profile to TOML."""
    lines: list[str] = []
    lines.append(f'name = "{_escape_toml_string(profile.name)}"')
    lines.append(f'instruction_template = "{_escape_toml_string(profile.instruction_template)}"')
    if profile.layout_profile:
        lines.append(f'layout_profile = "{_escape_toml_string(profile.layout_profile)}"')
    lines.append(f"symlinks = {_render_string_list(profile.symlinks)}")
    lines.append(f"rules = {_render_string_list(profile.rules)}")
    lines.append("")
    for skill in profile.skills:
        lines.append("[[skill]]")
        lines.append(f'qualified_name = "{_escape_toml_string(skill.qualified_name)}"')
        if skill.pin:
            lines.append(f'pin = "{_escape_toml_string(skill.pin)}"')
        if skill.track:
            lines.append(f'track = "{_escape_toml_string(skill.track)}"')
        lines.append("")
    for agent in profile.agents:
        lines.append("[[subagent]]")
        lines.append(f'qualified_name = "{_escape_toml_string(agent.qualified_name)}"')
        if agent.pin:
            lines.append(f'pin = "{_escape_toml_string(agent.pin)}"')
        if agent.track:
            lines.append(f'track = "{_escape_toml_string(agent.track)}"')
        lines.append("")
    for mcp in profile.mcp_servers:
        lines.append("[[mcp_server]]")
        lines.append(f'registry_name = "{_escape_toml_string(mcp.registry_name)}"')
        lines.append(f'alias = "{_escape_toml_string(mcp.alias)}"')
        if mcp.transport:
            lines.append(f'transport = "{_escape_toml_string(mcp.transport)}"')
        if mcp.overrides:
            lines.append("  [mcp_server.overrides]")
            for key, value in mcp.overrides.items():
                lines.append(f"  {key} = {_render_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _escape_toml_string(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        char = match.group(0)
        if char == "\\":
            return "\\\\"
        if char == '"':
            return '\\"'
        if char == "\n":
            return "\\n"
        if char == "\t":
            return "\\t"
        code = ord(char)
        return f"\\u{code:04x}"

    return re.sub(r'[\\"\x00-\x1f\x7f]', _replace, value)


def _render_string_list(values: list[str]) -> str:
    if not values:
        return "[]"
    parts = [f'"{_escape_toml_string(v)}"' for v in values]
    return "[" + ", ".join(parts) + "]"


def _render_toml_value(value: object) -> str:
    if isinstance(value, str):
        return f'"{_escape_toml_string(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if value is None:
        return "false"
    # Fall back to string for anything else.
    return f'"{_escape_toml_string(str(value))}"'


def from_project(name: str, project_root: Path) -> Profile:
    """Build a profile from a project's declarations and lock state."""
    from aim.core import declarations as declarations_mod
    from aim.core import manifest as manifest_mod
    from aim.core.models import DeclaredMcpServer

    _validate_name(name)

    # Declarations carry user-editable intent; the lock carries installed artifacts.
    decl = declarations_mod.load_or_default(project_root)
    try:
        m = manifest_mod.load(project_root)
    except manifest_mod.ManifestNotFoundError:
        m = None

    # Prefer declarations (user intent) for installed artifacts; fall back to the
    # lockfile for legacy projects that pre-date the two-file model.
    skill_sources = decl.skills if decl.skills else (m.skills if m else [])
    agent_sources = decl.agents if decl.agents else (m.agents if m else [])
    mcp_sources = decl.mcp_servers if decl.mcp_servers else (m.mcp_servers if m else [])

    return Profile(
        name=name,
        instruction_template=decl.instruction_template,
        layout_profile=decl.layout_profile,
        symlinks=list(decl.symlinks),
        rules=[r.qualified_name for r in decl.rules],
        skills=[
            ProfileSkill(qualified_name=s.qualified_name, pin=s.pin, track=s.track)
            for s in skill_sources
        ],
        agents=[
            ProfileAgent(qualified_name=a.qualified_name, pin=a.pin, track=a.track)
            for a in agent_sources
        ],
        mcp_servers=[
            ProfileMcpServer(
                registry_name=ms.registry_name,
                alias=ms.alias,
                transport=(
                    ms.preferred_transport
                    if isinstance(ms, DeclaredMcpServer)
                    else (ms.entry.type if ms.entry else None)
                ),
                overrides=ms.overrides,
            )
            for ms in mcp_sources
        ],
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
    installed_rules: list[str] = field(default_factory=list)
    skipped_rules: list[str] = field(default_factory=list)


def apply(name: str, project_root: Path, *, allow_insecure: bool = False) -> ProfileApplyResult:
    """Apply a profile to a project: init declarations, lock them, install skills/agents/MCP, sync."""
    profile = load(name)
    init_result = init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            instruction_template=profile.instruction_template,
            layout_profile=profile.layout_profile,
            symlinks=tuple(profile.symlinks),
        )
    )

    # Resolve declarations into a lock so subsequent installs preserve symlinks.
    asyncio.run(
        lock_mod.run(lock_mod.LockOptions(project_root=project_root, allow_insecure=allow_insecure))
    )

    installed_skills: list[str] = []
    skipped_skills: list[str] = []
    for ps in profile.skills:
        try:
            install_mod.install(project_root, ps.qualified_name, track=ps.track, pin=ps.pin)
            installed_skills.append(ps.qualified_name)
        except install_mod.SkillNotIndexedError:
            skipped_skills.append(ps.qualified_name)

    installed_agents: list[str] = []
    skipped_agents: list[str] = []
    for pa in profile.agents:
        try:
            agent_install_mod.install(project_root, pa.qualified_name, track=pa.track, pin=pa.pin)
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

    installed_rules: list[str] = []
    skipped_rules: list[str] = []
    for qn in profile.rules:
        try:
            rule_install_mod.install(project_root, qn)
            installed_rules.append(qn)
        except rule_install_mod.RuleNotIndexedError:
            skipped_rules.append(qn)

    # Reconcile agent instruction files so symlinks promised by the profile are
    # actually written to disk.
    asyncio.run(
        sync_mod.run(
            sync_mod.SyncOptions(
                project_root=project_root,
                sync_agents=True,
                layout_profile=profile.layout_profile,
                allow_insecure=allow_insecure,
            )
        )
    )

    return ProfileApplyResult(
        project_root=project_root,
        init_result=init_result,
        installed_skills=installed_skills,
        skipped_skills=skipped_skills,
        installed_agents=installed_agents,
        skipped_agents=skipped_agents,
        installed_mcp=installed_mcp,
        skipped_mcp=skipped_mcp,
        installed_rules=installed_rules,
        skipped_rules=skipped_rules,
    )
