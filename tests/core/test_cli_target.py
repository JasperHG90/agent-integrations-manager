"""CLI coverage for `aim target`."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aim import cli
from aim.core import manifest, repos
from tests.fixtures import git_fixtures

_runner = CliRunner()

_TARGET = """
name = "opencode"
[manifest]
file = "package.json"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def _register(tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"targets/opencode.toml": _TARGET, "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")


def test_target_list_and_add(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path)

    listed = _runner.invoke(cli.app, ["target", "list"])
    assert listed.exit_code == 0, listed.output
    assert "a/opencode" in listed.output

    monkeypatch.chdir(project_root)  # install into the current project
    added = _runner.invoke(cli.app, ["target", "add", "a/opencode"])
    assert added.exit_code == 0, added.output
    assert "added target a/opencode" in added.output
    assert (project_root / ".aim" / "targets" / "opencode.toml").exists()
    assert [t.qualified_name for t in manifest.load(project_root).targets] == ["a/opencode"]


def test_target_remove(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(tmp_path)
    monkeypatch.chdir(project_root)
    _runner.invoke(cli.app, ["target", "add", "a/opencode"])

    removed = _runner.invoke(cli.app, ["target", "remove", "a/opencode"])
    assert removed.exit_code == 0, removed.output
    assert manifest.load(project_root).targets == []


def test_target_view(home: Path, tmp_path: Path) -> None:
    _register(tmp_path)
    viewed = _runner.invoke(cli.app, ["target", "view", "a/opencode"])
    assert viewed.exit_code == 0
    assert 'name = "opencode"' in viewed.output
