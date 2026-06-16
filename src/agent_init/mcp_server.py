"""Minimal MCP (Model Context Protocol) server over stdio.

Exposes a project's installed skills to live agents (Claude Desktop, Cursor,
etc.) without pre-stuffing every skill body into the prompt.

Why hand-rolled instead of `pip install mcp`? Keeps deps small. We only need
three methods: `initialize`, `tools/list`, `tools/call`. Everything else
returns a method-not-found error and clients are tolerant of that.

Read-only by design: only `list_skills` and `get_skill` are exposed. If you
want to write through MCP, add it explicitly (this is a deliberate scope cap).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cachetools import TTLCache

from agent_init.core import manifest, mcp_registry, paths

SERVER_NAME = "agent-init"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# In-memory caches to keep repeated tools/list + tools/call fast during a session.
_MANIFEST_CACHE: TTLCache[str, manifest.Manifest] = TTLCache(maxsize=16, ttl=30)
_SKILL_BODY_CACHE: TTLCache[tuple[str, str], str] = TTLCache(maxsize=64, ttl=60)
_AGENT_BODY_CACHE: TTLCache[tuple[str, str], str] = TTLCache(maxsize=64, ttl=60)
_MCP_JSON_CACHE: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=16, ttl=30)


def _make_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_skills",
            "description": "List the skills installed in the active agent-init project.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_skill",
            "description": "Return the SKILL.md body of an installed skill, by qualified_name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "qualified_name": {
                        "type": "string",
                        "description": "<repo_alias>/<skill_name> matching one returned by list_skills.",
                    }
                },
                "required": ["qualified_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_agents",
            "description": "List the sub-agents installed in the active agent-init project.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_agent",
            "description": "Return the AGENT.md body of an installed sub-agent, by qualified_name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "qualified_name": {
                        "type": "string",
                        "description": "<repo_alias>/<agent_name> matching one returned by list_agents.",
                    }
                },
                "required": ["qualified_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_mcp_servers",
            "description": "List the MCP servers configured in the project's .mcp.json.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_mcp_server",
            "description": "Return the .mcp.json entry for a configured MCP server, by alias.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "alias": {
                        "type": "string",
                        "description": "Local alias matching one returned by list_mcp_servers.",
                    }
                },
                "required": ["alias"],
                "additionalProperties": False,
            },
        },
    ]


def _load_manifest_cached(project_root: Path) -> manifest.Manifest:
    key = str(project_root)
    cached = _MANIFEST_CACHE.get(key)
    if cached is not None:
        return cached
    m = manifest.load(project_root)
    _MANIFEST_CACHE[key] = m
    return m


def _read_skill_body_cached(project_root: Path, target_dir: str) -> str:
    key = (str(project_root), target_dir)
    cached = _SKILL_BODY_CACHE.get(key)
    if cached is not None:
        return cached
    body = (project_root / target_dir / "SKILL.md").read_text()
    _SKILL_BODY_CACHE[key] = body
    return body


def _read_agent_body_cached(project_root: Path, target_path: str) -> str:
    key = (str(project_root), target_path)
    cached = _AGENT_BODY_CACHE.get(key)
    if cached is not None:
        return cached
    body = (project_root / target_path).read_text()
    _AGENT_BODY_CACHE[key] = body
    return body


def _read_mcp_json_cached(project_root: Path) -> dict[str, Any]:
    key = str(project_root)
    cached = _MCP_JSON_CACHE.get(key)
    if cached is not None:
        return cached
    data = mcp_registry.read_mcp_json(project_root)
    _MCP_JSON_CACHE[key] = data
    return data


def _call_tool(name: str, args: dict[str, Any], project_root: Path) -> dict[str, Any]:
    try:
        m = _load_manifest_cached(project_root)
    except manifest.ManifestNotFoundError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No .agent-init/manifest.json at {project_root}. Run `agent-init init` first.",
                }
            ],
            "isError": True,
        }

    if name == "list_skills":
        lines = [
            f"{s.qualified_name}  ({s.current.identifier()})  -> {s.target_dir}"
            for s in m.skills
        ]
        body = "\n".join(lines) if lines else "(no skills installed)"
        return {"content": [{"type": "text", "text": body}]}

    if name == "get_skill":
        qn = args.get("qualified_name", "")
        match = next((s for s in m.skills if s.qualified_name == qn), None)
        if match is None:
            return {
                "content": [{"type": "text", "text": f"Skill not installed: {qn}"}],
                "isError": True,
            }
        target = paths.safe_project_path(project_root, match.target_dir, "SKILL.md")
        if target is None or not target.exists():
            return {
                "content": [{"type": "text", "text": f"SKILL.md missing or unsafe at {match.target_dir}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": _read_skill_body_cached(project_root, match.target_dir)}]}

    if name == "list_agents":
        lines = [
            f"{a.qualified_name}  ({a.current.identifier()})  -> {a.target_path}"
            for a in m.agents
        ]
        body = "\n".join(lines) if lines else "(no agents installed)"
        return {"content": [{"type": "text", "text": body}]}

    if name == "get_agent":
        qn = args.get("qualified_name", "")
        match = next((a for a in m.agents if a.qualified_name == qn), None)
        if match is None:
            return {
                "content": [{"type": "text", "text": f"Agent not installed: {qn}"}],
                "isError": True,
            }
        target = paths.safe_project_path(project_root, match.target_path)
        if target is None or not target.exists():
            return {
                "content": [{"type": "text", "text": f"AGENT.md missing or unsafe at {match.target_path}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": _read_agent_body_cached(project_root, match.target_path)}]}

    if name == "list_mcp_servers":
        try:
            data = _read_mcp_json_cached(project_root)
        except mcp_registry.McpRegistryError as exc:
            return {"content": [{"type": "text", "text": f"Invalid .mcp.json: {exc}"}], "isError": True}
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict) or not servers:
            return {"content": [{"type": "text", "text": "(no MCP servers configured)"}]}
        lines = [f"{alias}" for alias in sorted(servers.keys())]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    if name == "get_mcp_server":
        alias = args.get("alias", "")
        try:
            data = _read_mcp_json_cached(project_root)
        except mcp_registry.McpRegistryError as exc:
            return {"content": [{"type": "text", "text": f"Invalid .mcp.json: {exc}"}], "isError": True}
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict) or alias not in servers:
            return {
                "content": [{"type": "text", "text": f"MCP server not configured: {alias}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": json.dumps(servers[alias], indent=2, sort_keys=True)}]}

    return {
        "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        "isError": True,
    }


def _handle(req: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return _make_response(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            },
        )
    if method == "notifications/initialized":
        return None  # notification — no response
    if method == "tools/list":
        return _make_response(req_id, {"tools": _tools()})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        return _make_response(req_id, _call_tool(name, args, project_root))
    return _make_error(req_id, -32601, f"method not found: {method}")


def serve(project_root: Path | None = None) -> None:
    """Run the stdio server loop until EOF."""
    root = (project_root or Path.cwd()).expanduser().resolve()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(
                json.dumps(_make_error(None, -32700, "parse error")) + "\n"
            )
            sys.stdout.flush()
            continue
        response = _handle(req, root)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


# Test-only helper: run one request synchronously without touching stdio.
def handle_for_test(req: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    return _handle(req, project_root)
