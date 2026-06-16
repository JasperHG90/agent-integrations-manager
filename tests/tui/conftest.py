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
