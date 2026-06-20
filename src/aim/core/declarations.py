"""User-editable project declarations stored in `aim.toml`.

`aim init` creates this file; `aim lock` resolves it into `aim.lock.toml`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from aim.core import paths
from aim.core.models import CURRENT_DECLARATIONS_VERSION, ProjectDeclarations


class DeclarationsNotFoundError(FileNotFoundError):
    """Raised when no `aim.toml` exists at the expected project path."""


class DeclarationsVersionError(RuntimeError):
    """Raised when an `aim.toml` cannot be migrated to the current version."""


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Forward-migrate raw declarations to CURRENT_DECLARATIONS_VERSION.

    Args:
        raw: Parsed `aim.toml` mapping, possibly from an older manifest version.

    Returns:
        The same mapping mutated in place to the current manifest version.

    Raises:
        DeclarationsVersionError: If the version is not an int, is newer than
            supported, or lists pre-v3 rules by name (no automatic migration).
    """
    version = raw.get("manifest_version", 1)
    if not isinstance(version, int):
        raise DeclarationsVersionError(
            f"manifest_version must be int, got {type(version).__name__}"
        )
    if version > CURRENT_DECLARATIONS_VERSION:
        raise DeclarationsVersionError(
            f"aim.toml version {version} is newer than supported ({CURRENT_DECLARATIONS_VERSION}). "
            "Upgrade aim."
        )
    if version < 2:
        # v2 drops agent_dialect and adds rules_mode default on the active layout profile.
        raw.pop("agent_dialect", None)
        raw["manifest_version"] = 2
        version = 2
    if version < 3:
        # v3 makes rules repo-sourced, SHA-pinned artifacts. The pre-v3 format
        # listed rules by name (`rule = ["..."]`) against a local library that no
        # longer exists. Rule-less projects upgrade cleanly; projects that listed
        # rules by name must re-add them (there is no automatic migration).
        if raw.get("rules"):
            raise DeclarationsVersionError(
                "aim.toml lists rules by name (pre-v3). v3 makes rules repo-sourced. "
                "Re-add each rule via `aim rule add <git-url> <name>`."
            )
        raw["rules"] = []
        raw["manifest_version"] = 3
        version = 3
    if version < 4:
        # v4 adds the optional [policy] governance table. Additive.
        raw.setdefault("policy", {})
        raw["manifest_version"] = 4
        version = 4
    if version < 5:
        # v5 adds the optional [instruction_archetype] selection. Additive — absence
        # means the built-in instruction template is used.
        raw["manifest_version"] = 5
    return raw


# TOML uses singular array-of-table headers; the models use plural field names.
_TOML_READ_MAP = {
    "skill": "skills",
    "subagent": "agents",
    "mcp_server": "mcp_servers",
    "rule": "rules",
}
_TOML_WRITE_MAP = {v: k for k, v in _TOML_READ_MAP.items()}

# Match TOML table headers like [[skills]], [skills.current], [[skills.history]], etc.
_TABLE_HEADER_RE = re.compile(r"^(\[\[?)(\w+)((?:\.\w+)*)?(\]\]?)$")


def _singularize_table_headers(text: str) -> str:
    """Rewrite plural TOML table headers back to their singular on-disk form.

    Args:
        text: Serialized TOML using the models' plural field names.

    Returns:
        The TOML text with array-of-table headers singularized per
        `_TOML_WRITE_MAP`.
    """
    out: list[str] = []
    for line in text.splitlines():
        match = _TABLE_HEADER_RE.match(line.strip())
        if match:
            prefix, base, suffix, suffix_bracket = match.groups()
            singular = _TOML_WRITE_MAP.get(base)
            if singular is not None:
                line = f"{prefix}{singular}{suffix or ''}{suffix_bracket}"
        out.append(line)
    return "\n".join(out)


def load(project_root: Path) -> ProjectDeclarations:
    """Load and migrate the project's `aim.toml` into a validated model.

    Args:
        project_root: Directory whose `aim.toml` should be read.

    Returns:
        The parsed, migrated, and validated project declarations.

    Raises:
        DeclarationsNotFoundError: If no `aim.toml` exists at `project_root`.
    """
    path = paths.project_declarations_path(project_root)
    if not path.exists():
        raise DeclarationsNotFoundError(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for singular, plural in _TOML_READ_MAP.items():
        if singular in raw:
            raw[plural] = raw.pop(singular)
    migrated = _migrate(raw)
    return ProjectDeclarations.model_validate(migrated)


def load_or_default(project_root: Path) -> ProjectDeclarations:
    """Load the project's declarations, or return empty defaults if absent.

    Args:
        project_root: Directory whose `aim.toml` should be read.

    Returns:
        The loaded declarations, or a fresh empty `ProjectDeclarations`.
    """
    try:
        return load(project_root)
    except DeclarationsNotFoundError:
        return ProjectDeclarations()


def save(project_root: Path, declarations: ProjectDeclarations) -> None:
    """Serialize declarations to the project's `aim.toml`.

    Args:
        project_root: Directory whose `aim.toml` should be written.
        declarations: The declarations to persist.
    """
    path = paths.project_declarations_path(project_root)
    data = declarations.model_dump(mode="json", exclude_none=True)
    text = tomli_w.dumps(data)
    text = _singularize_table_headers(text)
    path.write_text(text + "\n", encoding="utf-8")


def _update_skill(project_root: Path, installed: object) -> None:
    """Mirror an installed skill into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledSkill` describing the skill to record.
    """
    from aim.core.models import DeclaredSkill, InstalledSkill

    assert isinstance(installed, InstalledSkill)
    decl = load_or_default(project_root)
    declared = DeclaredSkill(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        target_dir=installed.target_dir,
        pin=installed.pin,
        track=installed.track,
    )
    decl.skills = [s for s in decl.skills if s.qualified_name != installed.qualified_name]
    decl.skills.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def _prune_repo_if_unused(decl: ProjectDeclarations, alias: str) -> None:
    """Drop the `[repos]` binding for `alias` once nothing references it.

    Install paths add these bindings, so an orphaned one only lingers after the
    last artifact (skill, agent, or rule) from that repo is removed.

    Args:
        decl: The declarations to prune in place.
        alias: The repo alias to remove if unreferenced.
    """
    used = (
        any(s.repo_alias == alias for s in decl.skills)
        or any(a.repo_alias == alias for a in decl.agents)
        or any(r.repo_alias == alias for r in decl.rules)
        or (
            decl.instruction_archetype is not None
            and decl.instruction_archetype.repo_alias == alias
        )
    )
    if not used:
        decl.repos.pop(alias, None)


def _remove_skill(project_root: Path, qualified_name: str) -> None:
    """Drop a declared skill and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the skill to remove.
    """
    decl = load_or_default(project_root)
    decl.skills = [s for s in decl.skills if s.qualified_name != qualified_name]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def _update_agent(project_root: Path, installed: object) -> None:
    """Mirror an installed agent into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledAgent` describing the agent to record.
    """
    from aim.core.models import DeclaredAgent, InstalledAgent

    assert isinstance(installed, InstalledAgent)
    decl = load_or_default(project_root)
    declared = DeclaredAgent(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        target_path=installed.target_path,
        pin=installed.pin,
        track=installed.track,
    )
    decl.agents = [a for a in decl.agents if a.qualified_name != installed.qualified_name]
    decl.agents.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def _remove_agent(project_root: Path, qualified_name: str) -> None:
    """Drop a declared agent and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the agent to remove.
    """
    decl = load_or_default(project_root)
    decl.agents = [a for a in decl.agents if a.qualified_name != qualified_name]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def _update_rule(project_root: Path, installed: object) -> None:
    """Mirror an installed rule into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledRule` describing the rule to record.
    """
    from aim.core.models import DeclaredRule, InstalledRule

    assert isinstance(installed, InstalledRule)
    decl = load_or_default(project_root)
    declared = DeclaredRule(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        pin=installed.pin,
        track=installed.track,
    )
    decl.rules = [r for r in decl.rules if r.qualified_name != installed.qualified_name]
    decl.rules.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def set_instruction_archetype(project_root: Path, installed: object) -> None:
    """Record the selected instruction archetype (singleton) in `aim.toml`.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledArchetype` describing the selected archetype.
    """
    from aim.core.models import DeclaredArchetype, InstalledArchetype

    assert isinstance(installed, InstalledArchetype)
    decl = load_or_default(project_root)
    decl.instruction_archetype = DeclaredArchetype(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        pin=installed.pin,
        track=installed.track,
    )
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def clear_instruction_archetype(project_root: Path) -> None:
    """Clear the selected instruction archetype, pruning its repo binding if unused."""
    decl = load_or_default(project_root)
    previous = decl.instruction_archetype
    decl.instruction_archetype = None
    if previous is not None:
        _prune_repo_if_unused(decl, previous.repo_alias)
    save(project_root, decl)


def _remove_rule(project_root: Path, qualified_name: str) -> None:
    """Drop a declared rule and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the rule to remove.
    """
    decl = load_or_default(project_root)
    decl.rules = [r for r in decl.rules if r.qualified_name != qualified_name]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def _update_mcp(project_root: Path, installed: object) -> None:
    """Mirror an installed MCP server into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledMcpServer` describing the server to record.
    """
    from aim.core.models import DeclaredMcpServer, InstalledMcpServer

    assert isinstance(installed, InstalledMcpServer)
    decl = load_or_default(project_root)
    declared = DeclaredMcpServer(
        alias=installed.alias,
        registry_name=installed.registry_name,
        preferred_transport=installed.entry.type if installed.entry else None,
        overrides=installed.overrides or {},
    )
    decl.mcp_servers = [m for m in decl.mcp_servers if m.alias != installed.alias]
    decl.mcp_servers.append(declared)
    save(project_root, decl)


def _remove_mcp(project_root: Path, alias: str) -> None:
    """Drop a declared MCP server by its alias.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        alias: The alias of the MCP server to remove.
    """
    decl = load_or_default(project_root)
    decl.mcp_servers = [m for m in decl.mcp_servers if m.alias != alias]
    save(project_root, decl)
