"""CLI coverage for the provenance flags on skill/rule/subagent list commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aim import cli
from aim.core import repos
from tests.fixtures import git_fixtures

_runner = CliRunner()


def test_provenance_flags_on_skill_rule_subagent_lists(home: Path, tmp_path: Path) -> None:
    """Plugin-owned artifacts show by default; --standalone-only hides them, and
    --exclude-dot-claude hides .claude/-found ones, on skill/rule/subagent lists."""
    marketplace = {"name": "demo", "plugins": [{"name": "bundler", "source": "./bundler"}]}
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "bundler/.claude-plugin/plugin.json": json.dumps({"name": "bundler"}),
            "bundler/skills/pskill/SKILL.md": "# pskill\n",
            "bundler/rules/prule.md": "Prule.\n",
            "bundler/agents/pagent/AGENT.md": "# pagent\n",
            "skills/cskill/SKILL.md": "# cskill\n",
            ".claude/rules/drule.md": "Drule.\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("pm", f"file://{bare}")

    for group, owned in (("skill", "pm/pskill"), ("rule", "pm/prule"), ("subagent", "pm/pagent")):
        default = _runner.invoke(cli.app, [group, "list"])
        assert default.exit_code == 0, default.output
        assert owned in default.output, default.output  # plugin-owned shown by default
        hidden = _runner.invoke(cli.app, [group, "list", "--standalone-only"])
        assert owned not in hidden.output, hidden.output  # --standalone-only hides them

    # A standalone skill still shows under --standalone-only.
    standalone = _runner.invoke(cli.app, ["skill", "list", "--standalone-only"])
    assert "pm/cskill" in standalone.output

    canon_only = _runner.invoke(cli.app, ["rule", "list", "--exclude-dot-claude"])
    assert "pm/drule" not in canon_only.output


def test_standalone_only_empty_reports_hidden_count(home: Path, tmp_path: Path) -> None:
    """A --standalone-only list that hides everything explains why, instead of a
    bare "no skills indexed"."""
    marketplace = {"name": "demo", "plugins": [{"name": "bundler", "source": "./bundler"}]}
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "bundler/.claude-plugin/plugin.json": json.dumps({"name": "bundler"}),
            "bundler/skills/pskill/SKILL.md": "# pskill\n",  # only a plugin-owned skill
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("pm", f"file://{bare}")

    res = _runner.invoke(cli.app, ["skill", "list", "--standalone-only"])
    assert res.exit_code == 0, res.output
    assert "hidden by --standalone-only" in res.output

    # The hint also fires on the search path.
    res = _runner.invoke(cli.app, ["skill", "search", "pskill", "--standalone-only"])
    assert res.exit_code == 0, res.output
    assert "hidden by --standalone-only" in res.output


def test_exclude_dot_claude_empty_reports_hidden_count(home: Path, tmp_path: Path) -> None:
    """The hint also covers the --exclude-dot-claude branch."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={".claude/rules/drule.md": "Drule.\n"},  # only a .claude/-found rule
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("pm", f"file://{bare}")

    res = _runner.invoke(cli.app, ["rule", "list", "--exclude-dot-claude"])
    assert res.exit_code == 0, res.output
    assert "hidden by --exclude-dot-claude" in res.output
