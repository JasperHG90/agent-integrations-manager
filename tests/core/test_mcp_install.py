from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from agent_init.core import init, mcp_install, mcp_registry


def _http_payload(name: str, version: str = "1.0.0") -> dict:
    return {
        "servers": [
            {
                "server": {
                    "name": name,
                    "description": "test",
                    "version": version,
                    "packages": [],
                    "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
                }
            }
        ]
    }


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    mcp_registry._SEARCH_CACHE.clear()
    yield
    mcp_registry._SEARCH_CACHE.clear()


@respx.mock
def test_install_creates_mcp_json_entry(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("my-server"))
    )
    installed = mcp_install.install(project_root, "my-server", alias="my")
    assert installed.alias == "my"

    data = json.loads((project_root / ".mcp.json").read_text())
    assert data["mcpServers"]["my"]["type"] == "http"

    m = mcp_install.manifest.load(project_root)
    assert len(m.mcp_servers) == 1
    assert m.mcp_servers[0].registry_name == "my-server"


@respx.mock
def test_install_preserves_unmanaged_servers(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    existing = {"mcpServers": {"legacy": {"type": "stdio", "command": "echo"}}, "keep": True}
    (project_root / ".mcp.json").write_text(json.dumps(existing))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("new"))
    )
    mcp_install.install(project_root, "new", alias="new")

    data = json.loads((project_root / ".mcp.json").read_text())
    assert "legacy" in data["mcpServers"]
    assert data["keep"] is True


def test_install_rejects_invalid_alias(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    with pytest.raises(mcp_install.McpAliasInvalidError):
        mcp_install.install(project_root, "x", alias="Bad Alias")


@respx.mock
def test_install_rejects_unmanaged_conflict(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    (project_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"taken": {"type": "stdio", "command": "echo"}}})
    )
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("taken"))
    )
    with pytest.raises(mcp_install.McpAliasConflictError):
        mcp_install.install(project_root, "taken", alias="taken")

    # Force allows taking over.
    installed = mcp_install.install(project_root, "taken", alias="taken", force=True)
    assert installed.alias == "taken"


@respx.mock
def test_update_refuses_local_edits(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("srv", version="1.0.0"))
    )
    mcp_install.install(project_root, "srv", alias="srv")

    data = json.loads((project_root / ".mcp.json").read_text())
    data["mcpServers"]["srv"]["url"] = "https://tampered.example.com"
    (project_root / ".mcp.json").write_text(json.dumps(data))

    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("srv", version="1.0.0"))
    )
    with pytest.raises(mcp_install.McpLocalEditsError):
        mcp_install.update(project_root, "srv")

    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("srv", version="1.0.0"))
    )
    mcp_install.update(project_root, "srv", force=True)


@respx.mock
def test_delete_removes_entry(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("srv"))
    )
    mcp_install.install(project_root, "srv", alias="srv")
    mcp_install.delete(project_root, "srv")

    data = json.loads((project_root / ".mcp.json").read_text())
    assert "srv" not in data.get("mcpServers", {})
    m = mcp_install.manifest.load(project_root)
    assert m.mcp_servers == []


@respx.mock
def test_uninstall_removes_entry(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_http_payload("srv"))
    )
    mcp_install.install(project_root, "srv", alias="srv")
    mcp_install.delete(project_root, "srv")

    data = json.loads((project_root / ".mcp.json").read_text())
    assert "srv" not in data.get("mcpServers", {})
    m = mcp_install.manifest.load(project_root)
    assert m.mcp_servers == []


def test_delete_unknown_alias_raises(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    with pytest.raises(mcp_install.McpServerNotInstalledError):
        mcp_install.delete(project_root, "missing")


def _payload_with_url(name: str, version: str, url: str) -> dict:
    return {
        "servers": [
            {
                "server": {
                    "name": name,
                    "description": "test",
                    "version": version,
                    "packages": [],
                    "remotes": [{"type": "streamable-http", "url": url}],
                }
            }
        ]
    }


@respx.mock
def test_rollback_restores_previous_entry(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_payload_with_url("srv", "1.0.0", "https://v1.example.com"))
    )
    mcp_install.install(project_root, "srv", alias="srv")
    first_url = json.loads((project_root / ".mcp.json").read_text())["mcpServers"]["srv"]["url"]

    mcp_registry._SEARCH_CACHE.clear()
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_payload_with_url("srv", "2.0.0", "https://v2.example.com"))
    )
    mcp_install.update(project_root, "srv")
    second_url = json.loads((project_root / ".mcp.json").read_text())["mcpServers"]["srv"]["url"]
    assert second_url != first_url

    mcp_install.rollback(project_root, "srv")
    rolled_url = json.loads((project_root / ".mcp.json").read_text())["mcpServers"]["srv"]["url"]
    assert rolled_url == first_url
