"""Public MCP registry client and mapping to Claude Code `.mcp.json` entries.

Uses `stamina` for retries and `cachetools.TTLCache` for short in-memory caching
so rapid TUI searches and repeated CLI calls don't hammer the registry.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import stamina
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field

from agent_init.core.hashing import hash_text
from agent_init.core.models import McpClaudeEntry, McpServerVersion

_REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0/servers"
_SEARCH_TTL_SECONDS = 60
_SEARCH_CACHE = TTLCache(maxsize=128, ttl=_SEARCH_TTL_SECONDS)
_TIMEOUT_SECONDS = 15


class McpRegistryError(Exception):
    """Registry fetch or response failed."""


class McpMappingError(Exception):
    """A registry server cannot be mapped to a Claude Code `.mcp.json` entry."""


class McpRemoteHeader(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    value: str | None = None
    description: str | None = None


class McpRemote(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    url: str
    headers: list[McpRemoteHeader] = Field(default_factory=list)


class McpEnvVar(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    default: str | None = None
    is_secret: bool = Field(default=False, alias="isSecret")


class McpPackage(BaseModel):
    model_config = ConfigDict(extra="allow")

    registry_type: str = Field(alias="registryType")
    identifier: str
    version: str | None = None
    runtime_hint: str | None = Field(default=None, alias="runtimeHint")
    transport: dict[str, str] = Field(default_factory=dict)
    environment_variables: list[McpEnvVar] = Field(default_factory=list, alias="environmentVariables")


class McpServer(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    title: str | None = None
    version: str | None = None
    packages: list[McpPackage] = Field(default_factory=list)
    remotes: list[McpRemote] = Field(default_factory=list)


class McpSearchResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    server: McpServer
    meta: dict = Field(default_factory=dict, alias="_meta")


def _canonical_json(value: dict) -> str:
    """Deterministic JSON for stable hashing and comparison."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@stamina.retry(
    on=(httpx.HTTPError, httpx.TimeoutException, httpx.NetworkError),
    attempts=3,
    timeout=10,
)
def _fetch_json(url: str) -> dict:
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        response = client.get(url, headers={"Accept": "application/json, application/problem+json"})
        response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise McpRegistryError(f"invalid JSON from registry: {exc}") from exc


def search_registry(query: str, cursor: str | None = None) -> tuple[list[McpSearchResult], str | None]:
    """Search the public MCP registry.

    Returns `(results, next_cursor)`. Results are cached in memory for 60s.
    """
    cache_key = (query.strip().lower(), cursor)
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    params: list[tuple[str, str]] = [("search", query)]
    if cursor:
        params.append(("cursor", cursor))
    query_string = "&".join(f"{k}={quote(v)}" for k, v in params)
    url = f"{_REGISTRY_BASE}?{query_string}"

    try:
        payload = _fetch_json(url)
    except Exception as exc:
        raise McpRegistryError(f"registry search failed: {exc}") from exc

    results = [McpSearchResult.model_validate(item) for item in payload.get("servers", [])]
    metadata = payload.get("metadata", {})
    next_cursor = metadata.get("nextCursor")
    out = (results, next_cursor)
    _SEARCH_CACHE[cache_key] = out
    return out


def _is_latest(meta: dict) -> bool:
    official = meta.get("io.modelcontextprotocol.registry/official", {})
    return bool(official.get("isLatest", False))


def _version_key(server: McpServer) -> tuple[int, ...]:
    """Best-effort numeric version sort key. Non-numeric parts sort as 0."""
    if not server.version:
        return (0,)
    out: list[int] = []
    for part in server.version.split("."):
        digits = re.match(r"^(\d+)", part)
        out.append(int(digits.group(1)) if digits else 0)
    return tuple(out)


def find_server(query: str, exact_name: str | None = None) -> McpServer:
    """Search the registry and return the single best-matching server.

    If `exact_name` is provided, only results whose `server.name` equals it
    are considered. Prefers the registry-flagged latest version, then the
    highest semantic-looking version.
    """
    results, _ = search_registry(query)
    candidates = results
    if exact_name:
        candidates = [r for r in results if r.server.name == exact_name]
    if not candidates:
        raise McpRegistryError(
            f"no MCP server found for {query!r}"
            + (f" with name {exact_name!r}" if exact_name else "")
        )
    latest = [r for r in candidates if _is_latest(r.meta)]
    chosen = latest[0] if latest else max(candidates, key=lambda r: _version_key(r.server))
    return chosen.server


def _choose_remote(server: McpServer, preferred_transport: str | None) -> McpRemote | None:
    if not server.remotes:
        return None
    if preferred_transport:
        normalized = preferred_transport.lower()
        for remote in server.remotes:
            if remote.type.lower() == normalized or (
                normalized == "http" and remote.type.lower() == "streamable-http"
            ):
                return remote
    precedence = ["streamable-http", "http", "sse", "ws"]
    for transport in precedence:
        for remote in server.remotes:
            if remote.type.lower() == transport:
                return remote
    return None


def _build_stdio_entry(package: McpPackage) -> McpClaudeEntry:
    registry_type = package.registry_type.lower()
    runtime_hint = (package.runtime_hint or "").lower()

    if runtime_hint == "uvx" or registry_type == "pypi":
        command = "uvx"
        args = [package.identifier]
    elif runtime_hint == "npx" or registry_type == "npm":
        command = "npx"
        args = ["-y", package.identifier]
    else:
        # Default to npx for unknown registry types; better than failing.
        command = "npx"
        args = ["-y", package.identifier]

    env: dict[str, str] = {}
    for var in package.environment_variables:
        if var.default is not None and not var.is_secret:
            env[var.name] = var.default

    return McpClaudeEntry(type="stdio", command=command, args=args, env=env)


def _build_remote_entry(remote: McpRemote) -> McpClaudeEntry:
    server_type = "http" if remote.type.lower() == "streamable-http" else remote.type.lower()
    headers: dict[str, str] = {}
    for header in remote.headers:
        if header.value is not None:
            headers[header.name] = header.value
    return McpClaudeEntry(type=server_type, url=remote.url, headers=headers)


def map_to_claude_entry(
    server: McpServer, *, preferred_transport: str | None = None
) -> McpClaudeEntry:
    """Convert a registry server definition to a Claude Code `.mcp.json` entry.

    Transport precedence:
      1. `preferred_transport` if the server has a matching remote/package.
      2. Remote precedence: streamable-http/http -> http, sse -> sse, ws -> ws.
      3. A stdio package derived from `runtimeHint`/`registryType`.

    Raises `McpMappingError` when no mapping is possible.
    """
    remote = _choose_remote(server, preferred_transport)
    if remote is not None:
        return _build_remote_entry(remote)

    if preferred_transport and preferred_transport.lower() == "stdio":
        for package in server.packages:
            if package.transport.get("type", "stdio").lower() == "stdio":
                return _build_stdio_entry(package)

    for package in server.packages:
        if package.transport.get("type", "stdio").lower() == "stdio":
            return _build_stdio_entry(package)

    raise McpMappingError(
        f"server {server.name!r} has no supported transport or stdio package"
    )


def hash_definition(server: McpServer) -> str:
    """Stable SHA256 of the raw registry server definition."""
    return hash_text(_canonical_json(server.model_dump(by_alias=True)))


def hash_entry(entry: McpClaudeEntry) -> str:
    """Stable SHA256 of the `.mcp.json` entry we will write."""
    return hash_text(_canonical_json(entry.model_dump(exclude_none=True)))


def make_mcp_server_version(server: McpServer, entry: McpClaudeEntry | None = None) -> McpServerVersion:
    return McpServerVersion(
        definition_hash=hash_definition(server),
        registry_version=server.version,
        installed_at=datetime.now(UTC),
        entry=entry,
    )


def read_mcp_json(project_root: Path) -> dict:
    """Read `.mcp.json` preserving unmanaged servers and top-level keys.

    Returns at least `{"mcpServers": {}}`.
    """
    path = project_root / ".mcp.json"
    if not path.exists():
        return {"mcpServers": {}}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {"mcpServers": {}}
    except json.JSONDecodeError as exc:
        raise McpRegistryError(f"invalid .mcp.json: {exc}") from exc
    if not isinstance(data, dict):
        raise McpRegistryError(".mcp.json must contain a JSON object")
    data.setdefault("mcpServers", {})
    return data


def write_mcp_json(project_root: Path, data: dict) -> Path:
    """Write `.mcp.json` with stable formatting."""
    path = project_root / ".mcp.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def merge_mcp_server(
    project_root: Path, alias: str, entry: McpClaudeEntry
) -> tuple[dict, McpClaudeEntry]:
    """Merge a managed server entry into `.mcp.json` and return the updated data
    plus the canonical entry that was written."""
    data = read_mcp_json(project_root)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpRegistryError(".mcp.json mcpServers must be a JSON object")
    servers[alias] = entry.model_dump(exclude_none=True)
    write_mcp_json(project_root, data)
    return data, entry


def remove_mcp_server(project_root: Path, alias: str) -> dict:
    """Remove a managed server alias from `.mcp.json`."""
    data = read_mcp_json(project_root)
    servers = data.get("mcpServers", {})
    if isinstance(servers, dict) and alias in servers:
        del servers[alias]
    write_mcp_json(project_root, data)
    return data
