"""Smoke tests for the MCP registry screen.

Avoids network by monkeypatching registry calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_init.core import mcp_registry
from agent_init.tui.app import AgentInitApp


def _server(name: str, version: str) -> mcp_registry.McpServer:
    return mcp_registry.McpServer(
        name=name,
        description=f"{name} description",
        version=version,
        packages=[],
        remotes=[],
    )


@pytest.mark.asyncio
async def test_mcp_screen_defaults_and_enter_search(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.widgets import DataTable, Input

    search_results = [
        mcp_registry.McpSearchResult(
            server=_server("io.github.hubertgajewski/playwright-report-mcp", "1.0.0"), meta={}
        ),
        mcp_registry.McpSearchResult(
            server=_server("io.github.hubertgajewski/playwright-report-mcp", "1.0.2"), meta={}
        ),
    ]

    def _find_server(name: str, exact_name: str | None = None) -> mcp_registry.McpServer:
        return _server("playwright-mcp", "1.0.0")

    def _search_registry(_query: str, _cursor: str | None = None) -> tuple[list, str | None]:
        return search_results, None

    monkeypatch.setattr(mcp_registry, "find_server", _find_server)
    monkeypatch.setattr(mcp_registry, "search_registry", _search_registry)

    app = AgentInitApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "McpScreen"

        table = app.screen.query_one(DataTable)
        # Defaults are shown initially (one row per unique name).
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "playwright-mcp"

        search = app.screen.query_one("#search-bar", Input)
        search.focus()
        search.value = "playwright-report"
        await pilot.press("enter")
        await pilot.pause()

        # Multiple versions of the same server collapse to one row.
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "io.github.hubertgajewski/playwright-report-mcp"
        assert table.get_row_at(0)[1] == "1.0.0"

        # Empty query + Enter restores defaults.
        search.value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "playwright-mcp"
