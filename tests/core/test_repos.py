from __future__ import annotations

from pathlib import Path

import pytest

from agent_init.core import repo_rules, repos
from tests.fixtures import git_fixtures


def test_add_clones_and_registers(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repo = repos.add("anthropic", f"file://{bare}")
    assert repo.alias == "anthropic"
    assert repo.last_sha is not None and len(repo.last_sha) == 40
    assert repos.clone_dir("anthropic").is_dir()
    assert (repos.clone_dir("anthropic") / "HEAD").is_file()


def test_add_rejects_bad_alias(home: Path) -> None:
    with pytest.raises(repos.RepoAliasError):
        repos.add("Bad Alias", "file:///tmp/nope")


def test_add_duplicate_errors(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("anthropic", f"file://{bare}")
    with pytest.raises(repos.RepoExistsError):
        repos.add("anthropic", f"file://{bare}")


def test_list_and_get(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("a", f"file://{bare}")
    repos.add("b", f"file://{bare}")
    aliases = [r.alias for r in repos.list_repos()]
    assert aliases == ["a", "b"]
    assert repos.get("a").alias == "a"


def test_remove_removes_clone(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("doomed", f"file://{bare}")
    assert repos.clone_dir("doomed").exists()
    repos.remove("doomed")
    assert not repos.clone_dir("doomed").exists()
    with pytest.raises(repos.RepoNotFoundError):
        repos.get("doomed")


def test_rename_moves_clone(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("old", f"file://{bare}")
    old_dir = repos.clone_dir("old")
    assert old_dir.exists()
    repos.rename("old", "new")
    assert not old_dir.exists()
    assert repos.clone_dir("new").exists()
    assert repos.get("new").alias == "new"


def test_rename_to_existing_errors(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("a", f"file://{bare}")
    repos.add("b", f"file://{bare}")
    with pytest.raises(repos.RepoExistsError):
        repos.rename("a", "b")


def test_refresh_updates_last_sha_on_new_commit(
    home: Path, bare_remote: tuple[Path, Path]
) -> None:
    working, bare = bare_remote
    repo = repos.add("anthropic", f"file://{bare}")
    initial_sha = repo.last_sha

    new_sha = git_fixtures.add_commit(working, {"newfile.md": "hi"}, "add newfile")
    git_fixtures.push_to_bare(working, bare)

    refreshed = repos.refresh("anthropic")
    assert refreshed.last_sha != initial_sha
    assert refreshed.last_sha == new_sha
    assert refreshed.last_fetched_at is not None


def _build_repo_with(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def test_add_indexes_rules(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/style.md": "# Style\n\nBe consistent.\n",
            "README.md": "x\n",
        },
    )
    repos.add("r", f"file://{bare}")
    assert repos.artifact_kinds("r") == {"rules"}
    rows = repo_rules.list_rules("r")
    assert [row.rule_name for row in rows] == ["style"]
    assert rows[0].title == "Style"


def test_add_indexes_rules_alongside_skills(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/foo/SKILL.md": "# Foo\n",
            "rules/r.md": "# R\n",
            "README.md": "x\n",
        },
    )
    repos.add("mixed", f"file://{bare}")
    assert repos.artifact_kinds("mixed") == {"skill", "rules"}


def test_rules_precedence_shadows_claude_path(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/x.md": "# canonical\n",
            ".claude/rules/x.md": "# shadow\n",
            "README.md": "x\n",
        },
    )
    repos.add("prec", f"file://{bare}")
    rows = repo_rules.list_rules("prec")
    assert [row.rule_name for row in rows] == ["x"]
    assert rows[0].rule_md_path == "rules/x.md"


def test_refresh_reindexes_rules(home: Path, tmp_path: Path) -> None:
    working, bare = _build_repo_with(
        tmp_path,
        {"rules/a.md": "a\n", "README.md": "x\n"},
    )
    repos.add("r", f"file://{bare}")
    assert {row.rule_name for row in repo_rules.list_rules("r")} == {"a"}

    git_fixtures.add_commit(working, {"rules/b.md": "b\n"}, "add b")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("r")
    assert {row.rule_name for row in repo_rules.list_rules("r")} == {"a", "b"}


def test_remove_deletes_rule_index(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {"rules/a.md": "a\n", "README.md": "x\n"},
    )
    repos.add("r", f"file://{bare}")
    assert repo_rules.list_rules("r")
    repos.remove("r")
    assert repo_rules.list_rules("r") == []
