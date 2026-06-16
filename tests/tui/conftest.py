"""TUI test fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_init.core import layout_profiles


@pytest.fixture(autouse=True)
def _tui_default_layout_profile(home) -> Iterator[None]:  # type: ignore[no-untyped-def]
    """Set a global default layout profile so the TUI startup picker is bypassed."""
    layout_profiles.set_global_default(layout_profiles.BUILTIN_CLAUDE.name)
    yield


@pytest.fixture(autouse=True)
def _block_mcp_registry_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent the TUI's background MCP seeding worker from hitting the network.

    Tests that exercise registry functionality monkeypatch the specific calls
    they need; this just stops the app-mount worker from stalling teardown."""
    from agent_init.core import mcp_registry

    def _raise_disabled(_url: str) -> dict:
        raise mcp_registry.McpRegistryError("network disabled in TUI tests")

    monkeypatch.setattr(mcp_registry, "_fetch_json", _raise_disabled)
    yield
