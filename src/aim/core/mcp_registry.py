"""Public MCP registry client and mapping to Claude Code `.mcp.json` entries.

Uses `stamina` for retries, `cachetools.TTLCache` for search-result caching,
and `cachetools.LRUCache` for in-session default-server caching. Persisted
DB cache lets the TUI MCP screen open instantly on startup even when the
public registry is slow.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx
import stamina
from cachetools import LRUCache, TTLCache
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from aim.core import content_guard, db, layout_profiles
from aim.core.hashing import hash_text
from aim.core.models import McpClaudeEntry, McpServerCache, McpServerVersion

logger = logging.getLogger(__name__)

_REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0/servers"
_SEARCH_TTL_SECONDS = 60
_SEARCH_CACHE: TTLCache[tuple[str, str | None], tuple[list[McpSearchResult], str | None]] = (
    TTLCache(maxsize=128, ttl=_SEARCH_TTL_SECONDS)
)
_DEFAULT_CACHE_MAXSIZE = 64
_DEFAULT_CACHE: LRUCache[str, McpServer] = LRUCache(maxsize=_DEFAULT_CACHE_MAXSIZE)
_CACHE_LOCK = threading.Lock()
_DB_CACHE_TTL_DAYS = 7
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
    environment_variables: list[McpEnvVar] = Field(
        default_factory=list, alias="environmentVariables"
    )


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
def _fetch_json(url: str, *, allow_insecure: bool = False) -> dict:
    content_guard.require_secure_url(url, allow_insecure=allow_insecure)
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
        response = client.get(url, headers={"Accept": "application/json, application/problem+json"})
        response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise McpRegistryError(f"invalid JSON from registry: {exc}") from exc


def search_registry(
    query: str, cursor: str | None = None, *, allow_insecure: bool = False
) -> tuple[list[McpSearchResult], str | None]:
    """Search the public MCP registry.

    Returns `(results, next_cursor)`. Results are cached in memory for 60s.
    """
    cache_key = (query.strip().lower(), cursor)
    with _CACHE_LOCK:
        cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cast(tuple[list[McpSearchResult], str | None], cached)

    params: list[tuple[str, str]] = [("search", query)]
    if cursor:
        params.append(("cursor", cursor))
    query_string = "&".join(f"{k}={quote(v)}" for k, v in params)
    url = f"{_REGISTRY_BASE}?{query_string}"

    try:
        payload = _fetch_json(url, allow_insecure=allow_insecure)
    except Exception as exc:
        raise McpRegistryError(f"registry search failed: {exc}") from exc

    results = [McpSearchResult.model_validate(item) for item in payload.get("servers", [])]
    metadata = payload.get("metadata", {})
    next_cursor = metadata.get("nextCursor")
    out = (results, next_cursor)
    with _CACHE_LOCK:
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


def _default_cache_key(name: str) -> str:
    return name.strip().lower()


def _server_from_json(text: str) -> McpServer:
    return McpServer.model_validate(json.loads(text))


def _get_cached_default(name: str) -> McpServer | None:
    key = _default_cache_key(name)
    with _CACHE_LOCK:
        cached = _DEFAULT_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        with db.session() as session:
            row = session.get(McpServerCache, key)
        if row is not None:
            cutoff = datetime.now(UTC) - timedelta(days=_DB_CACHE_TTL_DAYS)
            if row.fetched_at.replace(tzinfo=UTC) >= cutoff:
                server = _server_from_json(row.definition_json)
                with _CACHE_LOCK:
                    _DEFAULT_CACHE[key] = server
                return server
    except Exception as exc:
        logger.debug("failed to read default MCP cache for %r: %s", name, exc)
    return None


def _set_cached_default(name: str, server: McpServer) -> None:
    key = _default_cache_key(name)
    with _CACHE_LOCK:
        _DEFAULT_CACHE[key] = server
    try:
        definition_json = json.dumps(server.model_dump(by_alias=True), sort_keys=True)
        with db.session() as session:
            existing = session.get(McpServerCache, key)
            if existing is None:
                session.add(
                    McpServerCache(
                        name=key,
                        definition_json=definition_json,
                        fetched_at=datetime.now(UTC),
                    )
                )
            else:
                existing.definition_json = definition_json
                existing.fetched_at = datetime.now(UTC)
            session.commit()
    except Exception as exc:
        logger.debug("failed to persist default MCP cache for %r: %s", name, exc)


def find_server(
    query: str,
    exact_name: str | None = None,
    *,
    prefer_cache: bool = True,
    allow_insecure: bool = False,
) -> McpServer:
    """Search the registry and return the single best-matching server.

    If `exact_name` is provided, only results whose `server.name` equals it
    are considered. Prefers the registry-flagged latest version, then the
    highest semantic-looking version.

    For exact-name lookups, checks the in-session LRUCache and the SQLite
    `McpServerCache` table before hitting the network (unless `prefer_cache`
    is `False`), and persists the fetched definition to both caches.
    """
    target = exact_name or query
    if exact_name and prefer_cache:
        cached = _get_cached_default(target)
        if cached is not None:
            return cached

    results, _ = search_registry(query, allow_insecure=allow_insecure)
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
    if exact_name:
        _set_cached_default(target, chosen.server)
    return chosen.server


def seed_default_servers(names: list[str]) -> dict[str, McpServer]:
    """Fetch and cache the given default server names, returning successes.

    Checks the in-session LRUCache and the SQLite `McpServerCache` table before
    hitting the network. Network failures are swallowed so startup seeding never
    crashes the TUI.
    """
    out: dict[str, McpServer] = {}
    for name in names:
        try:
            out[name] = find_server(name, exact_name=name)
        except McpRegistryError as exc:
            logger.debug("default MCP server %r not seeded: %s", name, exc)
    return out


def list_cached_servers() -> list[tuple[str, McpServer, datetime, datetime]]:
    """Return all non-expired cached server definitions from the DB.

    Each tuple is `(canonical_name, server, fetched_at, valid_until)`, sorted by
    `fetched_at` descending (most recent first). In-memory-only cache entries
    are also included and sorted to the top by most-recent use order.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=_DB_CACHE_TTL_DAYS)
    rows: list[tuple[str, McpServer, datetime, datetime]] = []
    try:
        with db.session() as session:
            for row in session.exec(select(McpServerCache)).all():
                if row.fetched_at.replace(tzinfo=UTC) >= cutoff:
                    rows.append(
                        (
                            row.name,
                            _server_from_json(row.definition_json),
                            row.fetched_at,
                            row.fetched_at + timedelta(days=_DB_CACHE_TTL_DAYS),
                        )
                    )
    except Exception as exc:
        logger.debug("failed to list cached MCP servers: %s", exc)

    # Merge any in-memory-only entries (e.g. from a recent install in this
    # process that may not have flushed to DB yet), preserving LRU order.
    memory_names = {name for name, _, _, _ in rows}
    with _CACHE_LOCK:
        for name in _DEFAULT_CACHE:
            if name not in memory_names:
                rows.append(
                    (name, _DEFAULT_CACHE[name], now, now + timedelta(days=_DB_CACHE_TTL_DAYS))
                )

    rows.sort(key=lambda item: item[2], reverse=True)
    return rows


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
        raise McpMappingError(
            f"unsupported MCP package type {package.registry_type!r} "
            f"(runtimeHint {package.runtime_hint!r})"
        )

    env: dict[str, str] = {}
    for var in package.environment_variables:
        if var.default is not None and not var.is_secret:
            env[var.name] = var.default

    return McpClaudeEntry(type="stdio", command=command, args=args, env=env)


def _build_remote_entry(remote: McpRemote, *, allow_insecure: bool = False) -> McpClaudeEntry:
    server_type = "http" if remote.type.lower() == "streamable-http" else remote.type.lower()
    content_guard.require_secure_url(remote.url, allow_insecure=allow_insecure)
    headers: dict[str, str] = {}
    for header in remote.headers:
        if header.value is not None:
            headers[header.name] = header.value
    return McpClaudeEntry(type=server_type, url=remote.url, headers=headers)


def map_to_claude_entry(
    server: McpServer, *, preferred_transport: str | None = None, allow_insecure: bool = False
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
        return _build_remote_entry(remote, allow_insecure=allow_insecure)

    if preferred_transport and preferred_transport.lower() == "stdio":
        for package in server.packages:
            if package.transport.get("type", "stdio").lower() == "stdio":
                return _build_stdio_entry(package)

    for package in server.packages:
        if package.transport.get("type", "stdio").lower() == "stdio":
            return _build_stdio_entry(package)

    raise McpMappingError(f"server {server.name!r} has no supported transport or stdio package")


def hash_definition(server: McpServer) -> str:
    """Stable SHA256 of the raw registry server definition."""
    return hash_text(_canonical_json(server.model_dump(by_alias=True)))


def hash_entry(entry: McpClaudeEntry) -> str:
    """Stable SHA256 of the `.mcp.json` entry we will write."""
    return hash_text(_canonical_json(entry.model_dump(exclude_none=True)))


def make_mcp_server_version(
    server: McpServer,
    entry: McpClaudeEntry | None = None,
    overrides: dict[str, object] | None = None,
) -> McpServerVersion:
    return McpServerVersion(
        definition_hash=hash_definition(server),
        registry_version=server.version,
        installed_at=datetime.now(UTC),
        entry=entry,
        overrides=overrides,
    )


def _mcp_json_path(project_root: Path) -> Path:
    profile = layout_profiles.resolve_active(project_root)
    return project_root / profile.mcp_json


def read_mcp_json(project_root: Path) -> dict:
    """Read `.mcp.json` preserving unmanaged servers and top-level keys.

    Returns at least `{"mcpServers": {}}`.
    """
    path = _mcp_json_path(project_root)
    if not path.exists():
        return {"mcpServers": {}}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {"mcpServers": {}}
    except json.JSONDecodeError as exc:
        raise McpRegistryError(f"invalid .mcp.json: {exc}") from exc
    if not isinstance(data, dict):
        raise McpRegistryError(".mcp.json must contain a JSON object")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpRegistryError(".mcp.json mcpServers must be a JSON object")
    data.setdefault("mcpServers", {})
    return data


def write_mcp_json(project_root: Path, data: dict) -> Path:
    """Write `.mcp.json` with stable formatting."""
    path = _mcp_json_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    entry_dict = entry.model_dump(exclude_none=True)
    content_guard.assert_no_hidden_unicode(
        json.dumps(entry_dict, ensure_ascii=False), source=f".mcp.json entry {alias}"
    )
    servers[alias] = entry_dict
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
