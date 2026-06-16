"""Default MCP servers the user wants pinned / included by default.

These are registry names; the TUI/CLI can resolve them through
`mcp_registry.find_server()`.
"""

from __future__ import annotations

DEFAULT_MCP_SERVER_NAMES: list[str] = [
    "playwright-mcp",
    "io.github.yohasacura/drawio-mcp",
]
