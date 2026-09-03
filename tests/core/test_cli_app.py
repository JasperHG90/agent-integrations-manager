"""CLI coverage for `aim app` (bump-manifest, unlock-db) and `aim check`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from aim import cli
from aim.core import declarations
from aim.core import init as init_mod
from aim.core import sync as sync_mod
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run
from aim.core.models import CURRENT_DECLARATIONS_VERSION

_runner = CliRunner()


def test_check_flags_deleted_region(home: Path, project_root: Path) -> None:
    """`aim check` must exit non-zero when a managed region was deleted outright
    (regression: only regions still PRESENT in the file were compared, so the
    pre-commit hook was green after the most complete drift possible)."""
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    asyncio.run(lock_run(LockOptions(project_root=project_root)))
    asyncio.run(sync_mod.run(sync_mod.SyncOptions(project_root=project_root)))

    res = _runner.invoke(cli.app, ["check", "--project", str(project_root)])
    assert res.exit_code == 0, res.output

    agents = project_root / "AGENTS.md"
    text = agents.read_text()
    start = text.index("<!-- BEGIN aim: header -->")
    end = text.index("<!-- END aim: header -->") + len("<!-- END aim: header -->")
    agents.write_text(text[:start] + text[end:])

    res = _runner.invoke(cli.app, ["check", "--project", str(project_root)])
    assert res.exit_code == 1
    assert "region 'header' deleted" in res.output


def test_bump_manifest_migrates_and_materializes_archetype(home: Path, project_root: Path) -> None:
    (project_root / "aim.toml").write_text("manifest_version = 7\n")

    res = _runner.invoke(cli.app, ["app", "bump-manifest", str(project_root)])

    assert res.exit_code == 0, res.output
    assert f"7 -> {CURRENT_DECLARATIONS_VERSION}" in res.output
    text = (project_root / "aim.toml").read_text()
    assert f"manifest_version = {CURRENT_DECLARATIONS_VERSION}" in text
    assert "[archetype]" in text
    assert declarations.load(project_root).archetype.is_builtin


def test_bump_manifest_is_idempotent(home: Path, project_root: Path) -> None:
    (project_root / "aim.toml").write_text("manifest_version = 7\n")
    _runner.invoke(cli.app, ["app", "bump-manifest", str(project_root)])

    res = _runner.invoke(cli.app, ["app", "bump-manifest", str(project_root)])

    assert res.exit_code == 0, res.output
    assert f"already at schema version {CURRENT_DECLARATIONS_VERSION}" in res.output


def test_unlock_db_runs(home: Path) -> None:
    res = _runner.invoke(cli.app, ["app", "unlock-db"])

    assert res.exit_code == 0, res.output
