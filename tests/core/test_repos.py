from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import content_guard, git, repo_rules, repos
from tests.fixtures import git_fixtures


class _AuthFailingBackend:
    """Fake git backend that always fails with an auth-related error."""

    def clone_bare(self, url: str, dest: Path) -> None:
        _ = dest
        raise git.GitError(f"fatal: Authentication failed for '{url}'")

    def fetch(self, repo_dir: Path) -> None:
        _ = repo_dir
        raise git.GitError("remote: Invalid username or password")

    def resolve_ref(self, repo_dir: Path, ref: str) -> str:
        _ = (repo_dir, ref)
        raise git.GitError("ref not found")

    def list_tags(self, repo_dir: Path) -> list:
        _ = repo_dir
        return []

    def latest_tag(self, repo_dir: Path, ref: str) -> None:
        _ = (repo_dir, ref)
        return None

    def ls_tree(self, repo_dir: Path, sha: str, path: str = "") -> list:
        _ = (repo_dir, sha, path)
        return []

    def cat_file(self, repo_dir: Path, sha: str, path: str) -> str:
        _ = (repo_dir, sha, path)
        raise git.GitError("not found")

    def cat_file_batch(self, repo_dir: Path, sha: str, paths: list[str]) -> dict[str, bytes]:
        _ = (repo_dir, sha, paths)
        raise git.GitError("not found")

    def cat_file_bytes(self, repo_dir: Path, sha: str, path: str) -> bytes:
        _ = (repo_dir, sha, path)
        raise git.GitError("not found")

    def archive(self, repo_dir: Path, sha: str, source_path: str, dest_dir: Path) -> None:
        _ = (repo_dir, sha, source_path, dest_dir)
        raise git.GitError("archive failed")

    def last_touching_sha(self, repo_dir: Path, ref: str, source_path: str) -> str:
        _ = (repo_dir, ref, source_path)
        raise git.GitError("not found")


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch):
    original = git.get_backend()
    backend = _AuthFailingBackend()
    git.set_backend(backend)
    yield backend
    git.set_backend(original)


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


def test_add_same_url_is_idempotent(home: Path, bare_remote: tuple[Path, Path]) -> None:
    # Re-adding the same upstream repo (by identity) reuses the existing record
    # instead of cloning a duplicate — even if the URL form/alias differ.
    _, bare = bare_remote
    first = repos.add("anthropic", f"file://{bare}")
    again = repos.add("anthropic", f"file://{bare}")
    assert again.alias == first.alias == "anthropic"
    # A different alias for the same URL still dedups to the first registration.
    deduped = repos.add("other", f"file://{bare}")
    assert deduped.alias == "anthropic"
    assert [r.alias for r in repos.list_repos()] == ["anthropic"]


def test_add_alias_reuse_for_different_url_errors(
    home: Path, bare_remote: tuple[Path, Path], tmp_path: Path
) -> None:
    _, bare = bare_remote
    repos.add("anthropic", f"file://{bare}")
    other = _build_repo_with(tmp_path, {"skills/x/SKILL.md": "# x\n"})[1]
    with pytest.raises(repos.RepoExistsError):
        repos.add("anthropic", f"file://{other}")


def test_list_and_get(home: Path, bare_remote: tuple[Path, Path], tmp_path: Path) -> None:
    _, bare = bare_remote
    other = _build_repo_with(tmp_path, {"skills/x/SKILL.md": "# x\n"})[1]
    repos.add("a", f"file://{bare}")
    repos.add("b", f"file://{other}")
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


def test_project_artifacts_for_repo_lists_without_removing(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import declarations, install

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n\nDescribed.\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    # Read-only: reports the declared artifact, leaves the install + declarations intact.
    assert repos.project_artifacts_for_repo(project_root, "a") == ["a/foo"]
    assert (project_root / ".claude" / "skills" / "foo").exists()
    assert "a" in declarations.load(project_root).repos


def test_project_artifacts_for_repo_no_declarations_is_empty(
    home: Path, project_root: Path
) -> None:
    assert repos.project_artifacts_for_repo(project_root, "anything") == []


def test_rename_moves_clone(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("old", f"file://{bare}")
    old_dir = repos.clone_dir("old")
    assert old_dir.exists()
    repos.rename("old", "new")
    assert not old_dir.exists()
    assert repos.clone_dir("new").exists()
    assert repos.get("new").alias == "new"


def test_rename_to_existing_errors(
    home: Path, bare_remote: tuple[Path, Path], tmp_path: Path
) -> None:
    _, bare = bare_remote
    other = _build_repo_with(tmp_path, {"skills/x/SKILL.md": "# x\n"})[1]
    repos.add("a", f"file://{bare}")
    repos.add("b", f"file://{other}")
    with pytest.raises(repos.RepoExistsError):
        repos.rename("a", "b")


def test_refresh_updates_last_sha_on_new_commit(home: Path, bare_remote: tuple[Path, Path]) -> None:
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


def test_add_indexes_nested_rules(home: Path, tmp_path: Path) -> None:
    """Rules may be grouped under sub-categories: rules/<category>/<name>.md."""
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/style/team-style.md": "Team style.\n",
            "rules/conduct/be-direct.md": "Be direct.\n",
            ".claude/rules/ops/runbook.md": "Runbook.\n",
            "README.md": "x\n",
        },
    )
    repos.add("team", f"file://{bare}")
    rows = repo_rules.list_rules("team")
    by_name = {row.rule_name: row.rule_md_path for row in rows}
    assert by_name == {
        "team-style": "rules/style/team-style.md",
        "be-direct": "rules/conduct/be-direct.md",
        "runbook": ".claude/rules/ops/runbook.md",
    }


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


def test_add_auth_failure_has_helpful_message(home: Path, fake_backend) -> None:
    with pytest.raises(git.GitError) as excinfo:
        repos.add("client", "https://github.client.example/client/repo.git")
    msg = str(excinfo.value)
    assert "client: failed to access https://github.client.example/client/repo.git" in msg
    assert "gh auth status (GH_HOST=github.client.example)" in msg
    assert "gh auth switch (GH_HOST=github.client.example)" in msg
    assert "gh auth setup-git (GH_HOST=github.client.example)" in msg


def test_refresh_auth_failure_has_helpful_message(
    home: Path, bare_remote: tuple[Path, Path]
) -> None:
    _, bare = bare_remote
    repos.add("client", f"file://{bare}")

    class _FetchFailingBackend:
        def clone_bare(self, url: str, dest: Path) -> None:
            _ = (url, dest)

        def fetch(self, repo_dir: Path) -> None:
            _ = repo_dir
            raise git.GitError("remote: Invalid username or password")

        def resolve_ref(self, repo_dir: Path, ref: str) -> str:
            _ = (repo_dir, ref)
            return "a" * 40

        def list_tags(self, repo_dir: Path) -> list:
            _ = repo_dir
            return []

        def latest_tag(self, repo_dir: Path, ref: str) -> None:
            _ = (repo_dir, ref)
            return None

        def ls_tree(self, repo_dir: Path, sha: str, path: str = "") -> list:
            _ = (repo_dir, sha, path)
            return []

        def cat_file(self, repo_dir: Path, sha: str, path: str) -> str:
            _ = (repo_dir, sha, path)
            raise git.GitError("not found")

        def cat_file_batch(self, repo_dir: Path, sha: str, paths: list[str]) -> dict[str, bytes]:
            _ = (repo_dir, sha, paths)
            raise git.GitError("not found")

        def cat_file_bytes(self, repo_dir: Path, sha: str, path: str) -> bytes:
            _ = (repo_dir, sha, path)
            raise git.GitError("not found")

        def archive(self, repo_dir: Path, sha: str, source_path: str, dest_dir: Path) -> None:
            _ = (repo_dir, sha, source_path, dest_dir)
            raise git.GitError("archive failed")

        def last_touching_sha(self, repo_dir: Path, ref: str, source_path: str) -> str:
            _ = (repo_dir, ref, source_path)
            raise git.GitError("not found")

    git.set_backend(_FetchFailingBackend())
    try:
        # Override the stored URL so refresh sees a GitHub Enterprise-style URL.
        from aim.core import db

        with db.session() as session:
            repo = session.get(repos.RegisteredRepo, "client")
            assert repo is not None
            repo.url = "https://github.client.example/client/repo.git"
            session.add(repo)
            session.commit()
        with pytest.raises(git.GitError) as excinfo:
            repos.refresh("client")
        msg = str(excinfo.value)
        assert "client: failed to access https://github.client.example/client/repo.git" in msg
        assert "gh auth status (GH_HOST=github.client.example)" in msg
    finally:
        git.reset_backend()


def test_add_rejects_http_transport(home: Path) -> None:
    with pytest.raises(content_guard.InsecureTransportError):
        repos.add("demo", "http://example.com/repo.git")


def test_add_allows_http_with_allow_insecure(home: Path, fake_backend) -> None:
    # When allow_insecure=True the URL is accepted and passed to git.
    # The fake backend fails with an auth error, proving we did not reject it early.
    with pytest.raises(git.GitError):
        repos.add("demo", "http://example.com/repo.git", allow_insecure=True)


def test_refresh_rejects_http_transport(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("demo", f"file://{bare}")
    from aim.core import db

    with db.session() as session:
        row = session.get(repos.RegisteredRepo, "demo")
        assert row is not None
        row.url = "http://example.com/repo.git"
        session.add(row)
        session.commit()
    with pytest.raises(content_guard.InsecureTransportError):
        repos.refresh("demo")


def test_refresh_many_fetches_in_parallel_and_reindexes(home: Path, tmp_path: Path) -> None:
    src_a = git_fixtures.make_source_repo(
        tmp_path / "src-a", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare_a = git_fixtures.make_bare_remote(src_a, tmp_path / "bare-a.git")
    src_b = git_fixtures.make_source_repo(
        tmp_path / "src-b", files={"skills/bar/SKILL.md": "# bar\n"}
    )
    bare_b = git_fixtures.make_bare_remote(src_b, tmp_path / "bare-b.git")
    repos.add("a", f"file://{bare_a}")
    repos.add("b", f"file://{bare_b}")

    # Advance repo a upstream; refresh_many must fetch + reindex it.
    git_fixtures.add_commit(src_a, {"skills/baz/SKILL.md": "# baz\n"}, "add baz")
    git_fixtures.push_to_bare(src_a, bare_a)

    results = repos.refresh_many(["a", "b"])
    by_alias = {alias: (repo, err) for alias, repo, err in results}
    assert by_alias["a"][1] is None and by_alias["b"][1] is None

    from aim.core import skills

    assert "a/baz" in {s.qualified_name for s in skills.list_skills("a")}


def test_refresh_many_reports_per_repo_failure(home: Path, tmp_path: Path) -> None:
    import shutil

    src = git_fixtures.make_source_repo(tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"})
    bare = git_fixtures.make_bare_remote(src, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    shutil.rmtree(bare)  # remote is gone; fetch will fail

    results = repos.refresh_many(["a"])
    alias, repo, err = results[0]
    assert alias == "a" and repo is None and err is not None
