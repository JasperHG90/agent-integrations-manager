"""CLI coverage for `view` subcommands and the consolidated `update` arg guard."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from aim import cli
from aim.core import repos
from tests.fixtures import git_fixtures

_runner = CliRunner()


def _repo_with_all_kinds(tmp_path: Path) -> str:
    """Register one repo holding a skill, sub-agent, rule, and instruction archetype."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# Foo Skill\n\nDo foo.\n",
            "agents/review/AGENT.md": "---\nname: Review\n---\n# Review Agent\n",
            "rules/be-concise.md": "Be concise always.\n",
            "instructions/lean/AGENTS.md": "# Lean Base\n\nBe terse.\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return f"file://{bare}"


def test_view_prints_source_for_each_kind(home: Path, tmp_path: Path) -> None:
    repos.add("a", _repo_with_all_kinds(tmp_path))

    cases = [
        (["skill", "view", "a/foo"], "Foo Skill"),
        (["subagent", "view", "a/review"], "Review Agent"),
        (["rule", "view", "a/be-concise"], "Be concise always."),
        (["instructions", "view", "a/lean"], "Lean Base"),
    ]
    for argv, needle in cases:
        result = _runner.invoke(cli.app, argv)
        assert result.exit_code == 0, f"{argv} exited {result.exit_code}: {result.output}"
        assert needle in result.output, f"{argv} output missing {needle!r}: {result.output}"


def test_instructions_view_strips_frontmatter(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"instructions/lean/AGENTS.md": "---\ntitle: Lean\n---\n# Real Body\n"},
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}", allow_empty=True)

    result = _runner.invoke(cli.app, ["instructions", "view", "a/lean"])
    assert result.exit_code == 0
    assert "# Real Body" in result.output
    assert "title: Lean" not in result.output


def test_view_unknown_name_is_friendly_error(home: Path, tmp_path: Path) -> None:
    repos.add("a", _repo_with_all_kinds(tmp_path))
    result = _runner.invoke(cli.app, ["rule", "view", "a/nope"])
    assert result.exit_code == 1  # _friendly maps NotIndexedError to a clean exit


def test_update_without_target_is_rejected(home: Path) -> None:
    for kind in ("skill", "subagent", "rule"):
        result = _runner.invoke(cli.app, [kind, "update"])
        assert result.exit_code != 0, f"{kind} update with no target should fail"
        assert "pass a <name>" in result.output, result.output
