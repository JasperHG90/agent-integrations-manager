"""MCP server install / update / delete / rollback.

Manages named entries in the project's `.mcp.json` file and records them in
`.agent-init/manifest.json`. Merge semantics preserve unmanaged servers and
top-level keys.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agent_init.core import hashing, manifest, mcp_registry, validation
from agent_init.core.models import InstalledMcpServer, Manifest, McpClaudeEntry


class McpAliasInvalidError(ValueError):
    """The requested alias does not pass repo-style alias validation."""


class McpAliasConflictError(ValueError):
    """The alias is already used by another managed or unmanaged server."""


class McpServerNotInstalledError(KeyError):
    """No entry for this alias in the project manifest."""


class McpNoHistoryToRollbackError(RuntimeError):
    pass


class McpLocalEditsError(RuntimeError):
    """The `.mcp.json` entry has been hand-edited. Pass `force=True` to overwrite."""


class McpOverrideEntry(BaseModel):
    """CLI/TUI escape hatch for manual entry configuration."""

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None


def _load_manifest(project_root: Path) -> Manifest:
    return manifest.load_or_default(project_root)


def _find_installed(m: Manifest, alias: str) -> InstalledMcpServer | None:
    for s in m.mcp_servers:
        if s.alias == alias:
            return s
    return None


class McpOverrideError(ValueError):
    """An override value has an invalid shape for the target field."""


def _apply_overrides(entry: McpClaudeEntry, overrides: dict[str, object]) -> McpClaudeEntry:
    """Apply simple override hints to a mapped entry.

    Recognised keys: `command`, `url`, plus list/dict values for `args`,
    `env`, `headers` if passed as already-parsed objects. CLI helpers pass
    pre-parsed values; strings replace the corresponding scalar field.
    """
    data = entry.model_dump()
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "args":
            if not isinstance(value, list):
                raise McpOverrideError(f"override 'args' must be a list, got {type(value).__name__}")
            data[key] = [str(v) for v in value if v is not None]
        elif key in ("env", "headers"):
            if not isinstance(value, dict):
                raise McpOverrideError(f"override '{key}' must be a dict, got {type(value).__name__}")
            data[key] = {str(k): str(v) for k, v in value.items() if v is not None}
        elif key in ("command", "url", "type"):
            data[key] = str(value)
        else:
            raise McpOverrideError(f"unsupported override key: {key}")
    return McpClaudeEntry.model_validate(data)


def _check_alias_available(
    project_root: Path,
    alias: str,
    registry_name: str,
    force: bool,
) -> None:
    if not validation.is_valid_alias(alias):
        raise McpAliasInvalidError(
            f"alias {alias!r} invalid: must be lowercase alphanumeric, _, or -"
        )

    m = _load_manifest(project_root)
    managed = _find_installed(m, alias)
    if managed is not None and managed.registry_name != registry_name and not force:
        raise McpAliasConflictError(
            f"alias {alias!r} is already managed for {managed.registry_name!r}. "
            "Pass --force to reassign."
        )

    data = mcp_registry.read_mcp_json(project_root)
    servers = data.get("mcpServers", {})
    if isinstance(servers, dict) and alias in servers:
        # Unmanaged alias present in .mcp.json but not in our manifest.
        if managed is None and not force:
            raise McpAliasConflictError(
                f"alias {alias!r} already exists in .mcp.json (not managed by agent-init). "
                "Pass --force to take it over."
            )


def _check_local_edits(project_root: Path, installed: InstalledMcpServer, *, force: bool) -> None:
    if force:
        return
    data = mcp_registry.read_mcp_json(project_root)
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return
    current = servers.get(installed.alias)
    if current is None:
        return
    current_hash = hashing.hash_text(mcp_registry._canonical_json(current))
    if current_hash != installed.entry_hash:
        raise McpLocalEditsError(
            f"MCP alias {installed.alias!r} in .mcp.json has been edited since install. "
            "Pass --force to overwrite."
        )


def install(
    project_root: Path,
    registry_name: str,
    *,
    alias: str,
    preferred_transport: str | None = None,
    overrides: dict[str, object] | None = None,
    force: bool = False,
) -> InstalledMcpServer:
    """Install (or replace) a managed MCP server entry under `alias`.

    - `registry_name` is the canonical `server.name` from the registry.
    - `preferred_transport` may be `stdio`, `http`, `sse`, or `ws`.
    - `overrides` is an optional dict of pre-parsed CLI/TUI escape-hatch
      values (`command`, `args`, `env`, `url`, `headers`).
    """
    _check_alias_available(project_root, alias, registry_name, force=force)

    m = _load_manifest(project_root)
    existing = _find_installed(m, alias)
    if existing is not None:
        _check_local_edits(project_root, existing, force=force)

    server = mcp_registry.find_server(registry_name, exact_name=registry_name)
    entry = mcp_registry.map_to_claude_entry(server, preferred_transport=preferred_transport)
    if overrides:
        entry = _apply_overrides(entry, overrides)

    mcp_registry.merge_mcp_server(project_root, alias, entry)
    version = mcp_registry.make_mcp_server_version(server, entry=entry)

    if existing is None:
        installed = InstalledMcpServer(
            alias=alias,
            registry_name=registry_name,
            entry=entry,
            entry_hash=mcp_registry.hash_entry(entry),
            current=version,
        )
        m.mcp_servers.append(installed)
        result = installed
    else:
        existing.push_history(version)
        existing.registry_name = registry_name
        existing.entry = entry
        existing.entry_hash = mcp_registry.hash_entry(entry)
        result = existing
    manifest.save(project_root, m)
    return result


def update(
    project_root: Path,
    alias: str,
    *,
    force: bool = False,
) -> InstalledMcpServer:
    """Refresh a managed MCP server from the registry.

    Re-fetches the server by its recorded `registry_name`, rebuilds the mapped
    entry, and writes it back. Refuses if the `.mcp.json` entry has been hand
    edited unless `force=True`.
    """
    m = _load_manifest(project_root)
    installed = _find_installed(m, alias)
    if installed is None:
        raise McpServerNotInstalledError(alias)

    _check_local_edits(project_root, installed, force=force)

    server = mcp_registry.find_server(
        installed.registry_name,
        exact_name=installed.registry_name,
        prefer_cache=False,
    )
    new_entry = mcp_registry.map_to_claude_entry(server)
    version = mcp_registry.make_mcp_server_version(server, entry=new_entry)

    if mcp_registry.hash_entry(new_entry) == installed.entry_hash and version.definition_hash == installed.current.definition_hash:
        return installed

    mcp_registry.merge_mcp_server(project_root, alias, new_entry)
    installed.push_history(version)
    installed.entry = new_entry
    installed.entry_hash = mcp_registry.hash_entry(new_entry)
    manifest.save(project_root, m)
    return installed


def delete(project_root: Path, alias: str) -> None:
    """Remove a managed MCP server from `.mcp.json` and the manifest."""
    m = _load_manifest(project_root)
    installed = _find_installed(m, alias)
    if installed is None:
        raise McpServerNotInstalledError(alias)
    mcp_registry.remove_mcp_server(project_root, alias)
    m.mcp_servers = [s for s in m.mcp_servers if s.alias != alias]
    manifest.save(project_root, m)


def rollback(project_root: Path, alias: str) -> InstalledMcpServer:
    """Restore `history[0]` as the current `.mcp.json` entry."""
    m = _load_manifest(project_root)
    installed = _find_installed(m, alias)
    if installed is None:
        raise McpServerNotInstalledError(alias)
    if not installed.history:
        raise McpNoHistoryToRollbackError(alias)

    target_version = installed.history.pop(0)
    target_entry = target_version.entry
    if target_entry is None:
        raise McpNoHistoryToRollbackError(
            f"no recorded entry for previous version of {alias}"
        )

    mcp_registry.merge_mcp_server(project_root, alias, target_entry)
    installed.history.insert(0, installed.current)
    installed.current = target_version
    installed.entry = target_entry
    installed.entry_hash = mcp_registry.hash_entry(target_entry)
    manifest.save(project_root, m)
    return installed
