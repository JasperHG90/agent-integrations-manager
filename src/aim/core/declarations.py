"""User-editable project declarations stored in `aim.toml`.

`aim init` creates this file; `aim lock` resolves it into `aim.lock`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import tomli_w

from aim.core import paths
from aim.core.models import CURRENT_DECLARATIONS_VERSION, ProjectDeclarations


class DeclarationsNotFoundError(FileNotFoundError):
    pass


class DeclarationsVersionError(RuntimeError):
    pass


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
    path = paths.project_declarations_path(project_root)
    if not path.exists():
        raise DeclarationsNotFoundError(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for singular, plural in _TOML_READ_MAP.items():
        if singular in raw:
            raw[plural] = raw.pop(singular)
    version = raw.get("manifest_version", CURRENT_DECLARATIONS_VERSION)
    if version != CURRENT_DECLARATIONS_VERSION:
        raise DeclarationsVersionError(
            f"aim.toml version {version} is not supported; expected {CURRENT_DECLARATIONS_VERSION}"
        )
    return ProjectDeclarations.model_validate(raw)


def load_or_default(project_root: Path) -> ProjectDeclarations:
    try:
        return load(project_root)
    except DeclarationsNotFoundError:
        return ProjectDeclarations()


def save(project_root: Path, declarations: ProjectDeclarations) -> None:
    path = paths.project_declarations_path(project_root)
    data = declarations.model_dump(mode="json", exclude_none=True)
    text = tomli_w.dumps(data)
    text = _singularize_table_headers(text)
    path.write_text(text + "\n", encoding="utf-8")


def _update_skill(project_root: Path, installed: object) -> None:
    """Mirror an installed skill into the declarations file."""
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


def _remove_skill(project_root: Path, qualified_name: str) -> None:
    decl = load_or_default(project_root)
    decl.skills = [s for s in decl.skills if s.qualified_name != qualified_name]
    save(project_root, decl)


def _update_agent(project_root: Path, installed: object) -> None:
    """Mirror an installed agent into the declarations file."""
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
    decl = load_or_default(project_root)
    decl.agents = [a for a in decl.agents if a.qualified_name != qualified_name]
    save(project_root, decl)


def _update_mcp(project_root: Path, installed: object) -> None:
    """Mirror an installed MCP server into the declarations file."""
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
    decl = load_or_default(project_root)
    decl.mcp_servers = [m for m in decl.mcp_servers if m.alias != alias]
    save(project_root, decl)
