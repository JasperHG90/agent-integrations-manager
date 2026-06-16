from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from agent_init.core import mcp_registry


def _payload(servers: list[dict], next_cursor: str | None = None) -> dict:
    out: dict = {"servers": [{"server": s} for s in servers]}
    if next_cursor:
        out["metadata"] = {"nextCursor": next_cursor}
    return out


def _http_server(name: str = "fetch") -> dict:
    return {
        "name": name,
        "description": "HTTP fetch server",
        "version": "1.2.3",
        "packages": [],
        "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
    }


def _sse_server(name: str = "events") -> dict:
    return {
        "name": name,
        "description": "SSE server",
        "version": "2.0.0",
        "packages": [],
        "remotes": [{"type": "sse", "url": "https://example.com/sse"}],
    }


def _stdio_server(name: str = "filesystem") -> dict:
    return {
        "name": name,
        "description": "Filesystem MCP",
        "version": "3.1.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@modelcontextprotocol/server-filesystem",
                "version": "3.1.0",
                "runtimeHint": "npx",
                "transport": {"type": "stdio"},
                "environmentVariables": [
                    {"name": "ROOT", "default": "/tmp", "isSecret": False}
                ],
            }
        ],
        "remotes": [],
    }


def _pypi_stdio_server(name: str = "weather") -> dict:
    return {
        "name": name,
        "description": "Weather via uvx",
        "version": "0.5.0",
        "packages": [
            {
                "registryType": "pypi",
                "identifier": "mcp-weather",
                "runtimeHint": "uvx",
                "transport": {"type": "stdio"},
            }
        ],
        "remotes": [],
    }


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    mcp_registry._SEARCH_CACHE.clear()
    mcp_registry._DEFAULT_CACHE.clear()
    yield
    mcp_registry._SEARCH_CACHE.clear()
    mcp_registry._DEFAULT_CACHE.clear()


def test_map_http_remote() -> None:
    server = mcp_registry.McpServer.model_validate(_http_server())
    entry = mcp_registry.map_to_claude_entry(server)
    assert entry.type == "http"
    assert entry.url == "https://example.com/mcp"


def test_map_sse_remote() -> None:
    server = mcp_registry.McpServer.model_validate(_sse_server())
    entry = mcp_registry.map_to_claude_entry(server)
    assert entry.type == "sse"
    assert entry.url == "https://example.com/sse"


def test_map_stdio_npx() -> None:
    server = mcp_registry.McpServer.model_validate(_stdio_server())
    entry = mcp_registry.map_to_claude_entry(server)
    assert entry.type == "stdio"
    assert entry.command == "npx"
    assert entry.args == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert entry.env == {"ROOT": "/tmp"}


def test_map_stdio_uvx() -> None:
    server = mcp_registry.McpServer.model_validate(_pypi_stdio_server())
    entry = mcp_registry.map_to_claude_entry(server)
    assert entry.type == "stdio"
    assert entry.command == "uvx"
    assert entry.args == ["mcp-weather"]


def test_map_unknown_registry_defaults_to_npx() -> None:
    raw = _stdio_server()
    raw["packages"][0]["registryType"] = "docker"
    raw["packages"][0]["runtimeHint"] = None
    server = mcp_registry.McpServer.model_validate(raw)
    entry = mcp_registry.map_to_claude_entry(server)
    assert entry.command == "npx"
    assert entry.args == ["-y", "@modelcontextprotocol/server-filesystem"]


def test_preferred_transport_respected() -> None:
    raw = _http_server()
    raw["remotes"].append({"type": "sse", "url": "https://example.com/sse"})
    server = mcp_registry.McpServer.model_validate(raw)
    entry = mcp_registry.map_to_claude_entry(server, preferred_transport="sse")
    assert entry.type == "sse"


def test_mapping_error_when_no_transport() -> None:
    server = mcp_registry.McpServer(name="empty", packages=[], remotes=[])
    with pytest.raises(mcp_registry.McpMappingError):
        mcp_registry.map_to_claude_entry(server)


@respx.mock
def test_search_registry() -> None:
    route = respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_payload([_http_server()], next_cursor="abc"))
    )
    results, cursor = mcp_registry.search_registry("fetch")
    assert route.called
    assert len(results) == 1
    assert results[0].server.name == "fetch"
    assert cursor == "abc"


@respx.mock
def test_search_registry_uses_cache() -> None:
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_payload([_http_server()]))
    )
    mcp_registry.search_registry("fetch")
    mcp_registry.search_registry("fetch")
    # respx counts distinct matched requests; duplicate cached call should not hit network.
    assert len([r for r in respx.routes if r.called]) == 1


@respx.mock
def test_find_server_prefers_latest() -> None:
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(
            200,
            json=_payload(
                [
                    {"name": "a", "version": "1.0.0", "packages": [], "remotes": []},
                    {
                        "name": "a",
                        "version": "2.0.0",
                        "packages": [],
                        "remotes": [],
                        "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
                    },
                ]
            ),
        )
    )
    server = mcp_registry.find_server("a", exact_name="a")
    assert server.version == "2.0.0"


def test_read_write_mcp_json_preserve_unmanaged(project_root: Path) -> None:
    initial = {
        "mcpServers": {"manual": {"type": "stdio", "command": "echo", "args": ["hi"]}},
        "extraKey": True,
    }
    (project_root / ".mcp.json").write_text(json.dumps(initial))

    entry = mcp_registry.McpClaudeEntry(type="http", url="https://example.com")
    data, _ = mcp_registry.merge_mcp_server(project_root, "managed", entry)
    assert data["extraKey"] is True
    assert data["mcpServers"]["manual"]["command"] == "echo"
    assert data["mcpServers"]["managed"]["type"] == "http"

    data = mcp_registry.remove_mcp_server(project_root, "managed")
    assert "managed" not in data["mcpServers"]
    assert data["mcpServers"]["manual"]["command"] == "echo"


def test_hash_entry_excludes_none_fields() -> None:
    entry = mcp_registry.McpClaudeEntry(type="http", url="https://example.com")
    dumped = mcp_registry._canonical_json(entry.model_dump(exclude_none=True))
    assert "command" not in dumped
