from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import git
from tests.fixtures import git_fixtures


@pytest.fixture
def backend() -> git.RealGitBackend:
    return git.RealGitBackend()


def test_clone_bare_treats_dash_url_as_url_not_option(
    backend: git.RealGitBackend, tmp_path: Path
) -> None:
    # With the `--` separator, a URL starting with `-` is parsed as a URL.
    # Git will fail because it is not a valid repository, but it will not try
    # to interpret it as a command-line option.
    with pytest.raises(git.GitError):
        backend.clone_bare("-invalid.git", tmp_path / "dest")


def test_resolve_ref_rejects_dash_prefix(backend: git.RealGitBackend, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", {"README.md": "x\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    backend.clone_bare(f"file://{bare}", tmp_path / "clone")
    with pytest.raises(git.GitError, match="ref must not start with '-'"):
        backend.resolve_ref(tmp_path / "clone", "--evil")


def test_resolve_ref_accepts_normal_ref(backend: git.RealGitBackend, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", {"README.md": "x\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    backend.clone_bare(f"file://{bare}", tmp_path / "clone")
    sha = backend.resolve_ref(tmp_path / "clone", "HEAD")
    assert len(sha) == 40


def test_cat_file_batch_matches_per_file_reads(backend: git.RealGitBackend, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        {
            "SKILL.md": "title\n",
            "empty.txt": "",
            "nested/deep.md": "deep\n",
        },
    )
    # A binary blob with null bytes, newlines, and a header-looking line — these
    # would break any naive line-based parser; `--batch` frames by size instead.
    (working / "blob.bin").write_bytes(b"\x00\x01\n1234 blob 9\ntrailing\x00")
    git_fixtures._run(["git", "add", "."], working)
    git_fixtures._run(["git", "commit", "-q", "-m", "add binary"], working)

    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    clone = tmp_path / "clone"
    backend.clone_bare(f"file://{bare}", clone)
    sha = backend.resolve_ref(clone, "HEAD")

    paths = sorted(backend.ls_tree(clone, sha))
    batched = backend.cat_file_batch(clone, sha, paths)

    assert set(batched) == set(paths)
    for path in paths:
        assert batched[path] == backend.cat_file_bytes(clone, sha, path)
    assert batched["empty.txt"] == b""


def test_cat_file_batch_empty_paths_skips_git(backend: git.RealGitBackend, tmp_path: Path) -> None:
    assert backend.cat_file_batch(tmp_path / "no-such-repo", "deadbeef", []) == {}


def test_cat_file_batch_raises_on_missing_object(
    backend: git.RealGitBackend, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", {"README.md": "x\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    clone = tmp_path / "clone"
    backend.clone_bare(f"file://{bare}", clone)
    sha = backend.resolve_ref(clone, "HEAD")
    with pytest.raises(git.GitError, match="missing"):
        backend.cat_file_batch(clone, sha, ["does-not-exist.md"])


def test_cat_files_text_matches_per_file_reads(tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        {
            "a/SKILL.md": "# A\n\nfirst\n",
            "b/SKILL.md": "# B\n\nsecond\n",
            "empty.md": "",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    clone = tmp_path / "clone"
    backend = git.get_backend()
    backend.clone_bare(f"file://{bare}", clone)
    sha = backend.resolve_ref(clone, "HEAD")

    paths = sorted(p for p in backend.ls_tree(clone, sha) if p.endswith(".md"))
    texts = git.cat_files_text(clone, sha, paths)

    assert set(texts) == set(paths)
    for path in paths:
        assert texts[path] == backend.cat_file(clone, sha, path)


def test_cat_files_text_falls_back_and_skips_missing(tmp_path: Path) -> None:
    """A missing object aborts the batch, so it falls back to per-file reads and
    silently drops only the unreadable path."""
    working = git_fixtures.make_source_repo(tmp_path / "src", {"real.md": "here\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    clone = tmp_path / "clone"
    backend = git.get_backend()
    backend.clone_bare(f"file://{bare}", clone)
    sha = backend.resolve_ref(clone, "HEAD")

    texts = git.cat_files_text(clone, sha, ["real.md", "missing.md"])
    assert texts == {"real.md": "here\n"}


def test_cat_files_text_empty_paths_returns_empty(tmp_path: Path) -> None:
    assert git.cat_files_text(tmp_path / "no-such-repo", "deadbeef", []) == {}


def test_default_branch_returns_remote_default(backend: git.RealGitBackend, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", {"README.md": "x\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    backend.clone_bare(f"file://{bare}", tmp_path / "clone")
    assert backend.default_branch(tmp_path / "clone") == "main"


def test_commits_behind_counts_missing_commits(backend: git.RealGitBackend, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", {"README.md": "x\n"})
    git_fixtures.add_tag(working, "v0")  # tags the initial commit
    git_fixtures.add_commit(working, {"a.txt": "a\n"}, "c2")
    git_fixtures.add_commit(working, {"b.txt": "b\n"}, "c3")
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    backend.clone_bare(f"file://{bare}", tmp_path / "clone")
    clone = tmp_path / "clone"

    assert backend.commits_behind(clone, "v0", "main") == 2  # v0 is 2 behind main
    assert backend.commits_behind(clone, "main", "main") == 0  # same ref, no lag


def test_commits_behind_rejects_dash_prefix(backend: git.RealGitBackend, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", {"README.md": "x\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    backend.clone_bare(f"file://{bare}", tmp_path / "clone")
    with pytest.raises(git.GitError, match="ref must not start with '-'"):
        backend.commits_behind(tmp_path / "clone", "--evil", "main")
