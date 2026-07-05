"""CLI coverage for `aim repo reindex`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aim import cli
from aim.core import db, repo_rules, repos
from tests.fixtures import git_fixtures

_runner = CliRunner()


def _register_rule_repo(tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"rules/a.md": "a\n", "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("r", f"file://{bare}")


def _register_repo_tracking_stale_tag(tmp_path: Path) -> None:
    """Register `r` tracking tag `v0`, with `main` one rule ahead (1 commit behind)."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"rules/base.md": "base\n", "README.md": "x\n"}
    )
    git_fixtures.add_tag(working, "v0")
    git_fixtures.add_commit(working, {"rules/extra.md": "extra\n"}, "add extra rule")
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("r", f"file://{bare}", default_ref="v0")


def test_repo_reindex_restores_stale_index(home: Path, tmp_path: Path) -> None:
    _register_rule_repo(tmp_path)
    # Drop the index without moving the SHA, then reindex via the CLI.
    with db.session() as session:
        session.exec(repos._delete_rule_index("r"))
        session.commit()
    assert repo_rules.list_rules("r") == []

    res = _runner.invoke(cli.app, ["repo", "reindex", "r"])

    assert res.exit_code == 0, res.output
    assert "reindexed r" in res.output
    assert {row.rule_name for row in repo_rules.list_rules("r")} == {"a"}


def test_repo_reindex_unknown_alias_errors(home: Path) -> None:
    res = _runner.invoke(cli.app, ["repo", "reindex", "nope"])

    assert res.exit_code != 0


def test_repo_set_ref_changes_tracked_ref(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    assert repos.get("r").default_ref == "v0"

    res = _runner.invoke(cli.app, ["repo", "set-ref", "r", "main"])

    assert res.exit_code == 0, res.output
    assert "set r ref -> main" in res.output
    assert repos.get("r").default_ref == "main"
    # Reindex ran: the main-only rule is now discovered.
    assert {row.rule_name for row in repo_rules.list_rules("r")} == {"base", "extra"}


def test_repo_set_ref_bogus_ref_errors(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    res = _runner.invoke(cli.app, ["repo", "set-ref", "r", "no-such-ref"])

    assert res.exit_code != 0
    assert repos.get("r").default_ref == "v0"  # unchanged


def test_repo_refresh_warns_when_behind(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    res = _runner.invoke(cli.app, ["repo", "refresh", "r"])

    assert res.exit_code == 0, res.output
    assert "behind" in res.output
    assert "set-ref r main" in res.output


def test_repo_list_shows_behind_column(home: Path, tmp_path: Path) -> None:
    _register_repo_tracking_stale_tag(tmp_path)
    res = _runner.invoke(cli.app, ["--json", "repo", "list"])

    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    row = next(r for r in rows if r["alias"] == "r")
    assert row["behind"] == "1 behind main"
    assert row["default_ref"] == "v0"
    # --json stays a superset of the pre-feature shape: original model fields survive.
    assert "repo_id" in row
    assert "last_sha" in row
