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
    """The same --include-plugin-owned / --exclude-dot-claude flags work on the
    skill, rule, and subagent list commands."""
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

    for group, hidden in (("skill", "pm/pskill"), ("rule", "pm/prule"), ("subagent", "pm/pagent")):
        default = _runner.invoke(cli.app, [group, "list"])
        assert default.exit_code == 0, default.output
        assert hidden not in default.output
        revealed = _runner.invoke(cli.app, [group, "list", "--include-plugin-owned"])
        assert hidden in revealed.output, revealed.output

    canon_only = _runner.invoke(cli.app, ["rule", "list", "--exclude-dot-claude"])
    assert "pm/drule" not in canon_only.output
