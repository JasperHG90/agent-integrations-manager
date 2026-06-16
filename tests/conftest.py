"""Shared fixtures. The AGENT_INIT_HOME env var redirects all platformdirs
lookups to a tmp dir so tests never touch the real user data/cache/config dirs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_init.core import db, paths


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate all global agent-init state into tmp_path/home.

    This fixture is autouse so every test — even core tests that only ask for
    `project_root` — runs against a temporary SQLite DB instead of the user's
    production cache database.
    """
    home_dir = tmp_path / "home"
    monkeypatch.setenv("AGENT_INIT_HOME", str(home_dir))
    # Reset the cached engine *before* touching any code path that might
    # initialize it, then point paths at the tmp dir and create tables there.
    db.reset_engine()
    paths.ensure_global_dirs()
    db.reset_engine()
    _clear_mcp_caches()
    yield home_dir
    db.reset_engine()
    _clear_mcp_caches()


def _clear_mcp_caches() -> None:
    from agent_init.core import mcp_registry

    mcp_registry._SEARCH_CACHE.clear()
    mcp_registry._DEFAULT_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_mcp_caches() -> Iterator[None]:
    """Ensure every test starts with empty MCP caches so network mocks are authoritative."""
    _clear_mcp_caches()
    yield
    _clear_mcp_caches()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    proj = tmp_path / "project"
    proj.mkdir()
    return proj


@pytest.fixture
def bare_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a real local bare git remote with one commit and a `skills/foo/SKILL.md`.
    Returns (working_repo_path, bare_remote_path)."""
    from tests.fixtures import git_fixtures

    working = git_fixtures.make_source_repo(
        tmp_path / "src-repo",
        files={
            "README.md": "fixture\n",
            "skills/foo/SKILL.md": "# foo\n\nFoo skill.\n",
            "skills/foo/extra.md": "supporting content\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare-remote.git")
    return working, bare
