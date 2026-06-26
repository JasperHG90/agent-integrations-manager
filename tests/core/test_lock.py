"""Tests for `aim lock` — skip-write-when-unchanged behavior."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from aim.core import declarations, git, lock, manifest, repos
from aim.core import init as init_mod
from aim.core.models import DeclaredSkill, ProjectDeclarations, SkillVersion
from tests.fixtures import git_fixtures


def _run_lock(project_root: Path, *, force: bool = False) -> lock.LockResult:
    return asyncio.run(lock.run(lock.LockOptions(project_root=project_root, force=force)))


def _setup_project_with_skill(
    project_root: Path, tmp_path: Path, files: dict[str, str], *, pin: str | None = None
) -> tuple[Path, Path, str]:
    """Create a bare repo with `files`, register it, init the project, and write
    aim.toml declaring one skill `a/foo` at source_path='skills/foo'."""
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    _write_aim_toml(project_root, source_path="skills/foo", pin=pin)
    return working, bare, "a/foo"


def _repo_url(repo_alias: str) -> str:
    """Real clone URL of a registered repo; identity-keyed serialization needs the
    same URL the repo is registered under (a placeholder would resolve to a
    different repo_id and fail to round-trip)."""
    try:
        return repos.get(repo_alias).url
    except repos.RepoNotFoundError:
        return "file://placeholder"


def _write_aim_toml(
    project_root: Path,
    *,
    source_path: str = "skills/foo",
    target_dir: str = ".claude/skills/foo",
    pin: str | None = None,
    repo_alias: str = "a",
    qualified_name: str = "a/foo",
) -> None:
    decl = ProjectDeclarations(
        repos={repo_alias: _repo_url(repo_alias)},
        skills=[
            _decl_skill(qualified_name, repo_alias, source_path, target_dir, pin=pin),
        ],
    )
    declarations.save(project_root, decl)


def _decl_skill(
    qualified_name: str,
    repo_alias: str,
    source_path: str,
    target_dir: str = ".claude/skills/foo",
    *,
    pin: str | None = None,
) -> DeclaredSkill:
    return DeclaredSkill(
        qualified_name=qualified_name,
        repo_alias=repo_alias,
        source_path=source_path,
        target_dir=target_dir,
        pin=pin,
    )


def _load_manifest(project_root: Path) -> manifest.Manifest:
    return manifest.load(project_root)


def _lockfile_text(project_root: Path) -> str:
    return (project_root / "aim.lock.toml").read_text()


# ---------------------------------------------------------------------------
# Basic skip behavior
# ---------------------------------------------------------------------------


def test_lock_writes_on_first_run(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_project_with_skill(
        project_root,
        tmp_path,
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    result = _run_lock(project_root)
    assert result.unchanged is False
    assert (project_root / "aim.lock.toml").exists()
    m = _load_manifest(project_root)
    assert len(m.skills) == 1
    assert m.skills[0].qualified_name == "a/foo"


def test_lock_skips_second_run_when_unchanged(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _setup_project_with_skill(
        project_root,
        tmp_path,
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    _run_lock(project_root)
    before = _lockfile_text(project_root)
    before_mtime = os.stat(project_root / "aim.lock.toml").st_mtime_ns

    result = _run_lock(project_root)

    assert result.unchanged is True
    after = _lockfile_text(project_root)
    assert after == before, "lockfile content should be byte-identical when unchanged"
    after_mtime = os.stat(project_root / "aim.lock.toml").st_mtime_ns
    assert after_mtime == before_mtime, "lockfile should not be rewritten when unchanged"


def test_lock_force_writes_even_when_unchanged(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _setup_project_with_skill(
        project_root,
        tmp_path,
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    _run_lock(project_root)
    before = _lockfile_text(project_root)

    result = _run_lock(project_root, force=True)

    assert result.unchanged is False
    after = _lockfile_text(project_root)
    # File was rewritten; installed_at should advance (fresh datetime.now stamp).
    assert after != before


def test_lock_no_index_skips_catalog_refresh_but_still_locks(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-index` must not refresh the search catalog, yet declared artifacts
    still resolve (they're read straight from the repo, not the index)."""
    from aim.core import agents as agents_mod
    from aim.core import skills as skills_mod

    _setup_project_with_skill(
        project_root,
        tmp_path,
        files={"skills/foo/SKILL.md": "# foo\n"},
    )

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(skills_mod, "index_repo", lambda alias: calls.append(("skill", alias)))
    monkeypatch.setattr(agents_mod, "index_repo", lambda alias: calls.append(("agent", alias)))

    # Default path refreshes the catalog for each declared repo.
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root, force=True)))
    assert ("skill", "a") in calls and ("agent", "a") in calls

    # --no-index skips the refresh entirely.
    calls.clear()
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root, force=True, no_index=True)))
    assert calls == []

    # ...but the declared skill is still locked.
    m = _load_manifest(project_root)
    assert [s.qualified_name for s in m.skills] == ["a/foo"]


# ---------------------------------------------------------------------------
# Perf-skip correctness: source_path change with same SHA must recompute hash
# ---------------------------------------------------------------------------


def test_lock_source_path_change_same_sha_recomputes_content_hash(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """The bug-catching test: changing source_path on a pinned tag (same SHA)
    must NOT reuse the cached content_hash. Without the source_path check in
    the perf-skip predicate, the new lockfile would carry a stale hash for the
    new path, silently breaking drift detection downstream."""
    # Repo with two distinct skills at the same commit.
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n",
            "skills/bar/SKILL.md": "# bar — different content\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    git_fixtures.add_tag(working, "v1")
    # Push the tag into the bare remote.
    git_fixtures.push_to_bare(working, bare)

    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    _write_aim_toml(project_root, source_path="skills/foo", pin="v1")
    _run_lock(project_root)
    old_hash = _load_manifest(project_root).skills[0].content_hash

    # Change source_path, keep the same pin (same SHA).
    _write_aim_toml(project_root, source_path="skills/bar", pin="v1")
    result = _run_lock(project_root)

    assert result.unchanged is False, "source_path change must trigger a write"
    new_hash = _load_manifest(project_root).skills[0].content_hash
    assert new_hash != old_hash, "content_hash must change when source_path changes"
    assert new_hash is not None
    # And the new source_path is what got recorded.
    assert _load_manifest(project_root).skills[0].source_path == "skills/bar"


# ---------------------------------------------------------------------------
# Pin moved to a new SHA: write fires, other items' installed_at preserved
# ---------------------------------------------------------------------------


def test_lock_pin_tag_moved_to_same_sha_skips(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    git_fixtures.add_tag(working, "v1")
    git_fixtures.push_to_bare(working, bare)

    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    _write_aim_toml(project_root, source_path="skills/foo", pin="v1")
    _run_lock(project_root)

    result = _run_lock(project_root)
    assert result.unchanged is True


def test_lock_pin_tag_moved_to_new_sha_writes_and_preserves_other_installed_at(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Two repos, each with one skill. Move the tag on repo B; assert that
    # repo A's skill keeps its installed_at while repo B's advances.
    work_a = git_fixtures.make_source_repo(
        tmp_path / "src_a", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare_a = git_fixtures.make_bare_remote(work_a, tmp_path / "bare_a.git")
    git_fixtures.add_tag(work_a, "v1")
    git_fixtures.push_to_bare(work_a, bare_a)

    work_b = git_fixtures.make_source_repo(
        tmp_path / "src_b", files={"skills/bar/SKILL.md": "# bar\n"}
    )
    bare_b = git_fixtures.make_bare_remote(work_b, tmp_path / "bare_b.git")
    git_fixtures.add_tag(work_b, "v1")
    git_fixtures.push_to_bare(work_b, bare_b)

    repos.add("a", f"file://{bare_a}")
    repos.add("b", f"file://{bare_b}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))

    decl = ProjectDeclarations(
        repos={"a": _repo_url("a"), "b": _repo_url("b")},
        skills=[
            DeclaredSkill(
                qualified_name="a/foo",
                repo_alias="a",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
                pin="v1",
            ),
            DeclaredSkill(
                qualified_name="b/bar",
                repo_alias="b",
                source_path="skills/bar",
                target_dir=".claude/skills/bar",
                pin="v1",
            ),
        ],
    )
    declarations.save(project_root, decl)
    _run_lock(project_root)
    m1 = _load_manifest(project_root)
    a_installed_at = next(s for s in m1.skills if s.qualified_name == "a/foo").current.installed_at
    b_installed_at_before = next(
        s for s in m1.skills if s.qualified_name == "b/bar"
    ).current.installed_at

    # Move tag v1 on repo B to a new commit.
    git_fixtures.add_commit(work_b, {"skills/bar/SKILL.md": "# bar v2\n"}, "bump bar")
    # Move the v1 tag to the new HEAD.
    import subprocess

    subprocess.run(["git", "tag", "-f", "v1"], cwd=work_b, check=True, capture_output=True)
    git_fixtures.push_to_bare(work_b, bare_b)
    repos.refresh("b")

    result = _run_lock(project_root)
    assert result.unchanged is False

    m2 = _load_manifest(project_root)
    a_after = next(s for s in m2.skills if s.qualified_name == "a/foo")
    b_after = next(s for s in m2.skills if s.qualified_name == "b/bar")
    assert a_after.current.installed_at == a_installed_at, "unchanged skill must keep installed_at"
    assert b_after.current.installed_at != b_installed_at_before, (
        "moved skill must advance installed_at"
    )


# ---------------------------------------------------------------------------
# History preservation
# ---------------------------------------------------------------------------


def test_lock_preserves_history_for_unchanged_skills(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _setup_project_with_skill(
        project_root,
        tmp_path,
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    _run_lock(project_root)

    # Inject a fake history entry into the lockfile to simulate prior updates.
    m = _load_manifest(project_root)
    m.skills[0].history = [
        SkillVersion(tag="v0", sha="deadbeef" * 5, installed_at=m.skills[0].current.installed_at)
    ]
    manifest.save(project_root, m)

    # No-op re-lock should preserve the history.
    result = _run_lock(project_root)
    assert result.unchanged is True
    m2 = _load_manifest(project_root)
    assert len(m2.skills[0].history) == 1
    assert m2.skills[0].history[0].sha == "deadbeef" * 5


# ---------------------------------------------------------------------------
# aim.toml structural changes
# ---------------------------------------------------------------------------


def test_lock_target_dir_change_triggers_write(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _setup_project_with_skill(
        project_root,
        tmp_path,
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    _run_lock(project_root)

    _write_aim_toml(project_root, source_path="skills/foo", target_dir=".claude/skills/renamed")
    result = _run_lock(project_root)
    assert result.unchanged is False
    m = _load_manifest(project_root)
    assert m.skills[0].target_dir == ".claude/skills/renamed"


def test_lock_reorders_skills_triggers_write_preserves_installed_at(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    work = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n",
            "skills/bar/SKILL.md": "# bar\n",
        },
    )
    bare = git_fixtures.make_bare_remote(work, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))

    def _decl(order: list[str]) -> None:
        skills = [
            DeclaredSkill(
                qualified_name=name,
                repo_alias="a",
                source_path=f"skills/{name.split('/')[1]}",
                target_dir=f".claude/skills/{name.split('/')[1]}",
            )
            for name in order
        ]
        declarations.save(
            project_root, ProjectDeclarations(repos={"a": _repo_url("a")}, skills=skills)
        )

    _decl(["a/foo", "a/bar"])
    _run_lock(project_root)
    m1 = _load_manifest(project_root)
    foo_at = next(s for s in m1.skills if s.qualified_name == "a/foo").current.installed_at

    # Swap the order.
    _decl(["a/bar", "a/foo"])
    result = _run_lock(project_root)
    assert result.unchanged is False, "reordering must trigger a write"
    m2 = _load_manifest(project_root)
    assert [s.qualified_name for s in m2.skills] == ["a/bar", "a/foo"]
    foo_after = next(s for s in m2.skills if s.qualified_name == "a/foo")
    assert foo_after.current.installed_at == foo_at, "per-item installed_at must survive reorder"


# ---------------------------------------------------------------------------
# Partial errors: write fires, then LockError raised
# ---------------------------------------------------------------------------


def test_lock_partial_error_writes_and_raises(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # One good skill and one whose repo is unresolvable.
    work = git_fixtures.make_source_repo(tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"})
    bare = git_fixtures.make_bare_remote(work, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))

    decl = ProjectDeclarations(
        repos={"a": _repo_url("a")},
        skills=[
            DeclaredSkill(
                qualified_name="a/foo",
                repo_alias="a",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            ),
            DeclaredSkill(
                qualified_name="ghost/missing",
                repo_alias="ghost",  # never registered
                source_path="skills/x",
                target_dir=".claude/skills/x",
            ),
        ],
    )
    declarations.save(project_root, decl)

    with pytest.raises(lock.LockError):
        _run_lock(project_root)

    # Partial manifest still written.
    m = _load_manifest(project_root)
    names = {s.qualified_name for s in m.skills}
    assert "a/foo" in names
    assert "ghost/missing" not in names


# ---------------------------------------------------------------------------
# track: HEAD — upstream movement must be detected (resolve_ref always runs)
# ---------------------------------------------------------------------------


def test_lock_track_head_upstream_moved_writes(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    work = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo v1\n"}
    )
    bare = git_fixtures.make_bare_remote(work, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    # No pin, no track → defaults to HEAD.
    _write_aim_toml(project_root, source_path="skills/foo")
    _run_lock(project_root)
    sha1 = _load_manifest(project_root).skills[0].current.sha

    # Advance HEAD on the source and push.
    git_fixtures.add_commit(work, {"skills/foo/SKILL.md": "# foo v2\n"}, "bump")
    git_fixtures.push_to_bare(work, bare)
    repos.refresh("a")

    result = _run_lock(project_root)
    assert result.unchanged is False, "HEAD movement must trigger a write"
    sha2 = _load_manifest(project_root).skills[0].current.sha
    assert sha2 != sha1


# ---------------------------------------------------------------------------
# resolve_ref dedup: one rev-parse per unique (repo, ref) within a lock run
# ---------------------------------------------------------------------------


def test_resolve_ref_cached_dedups_within_run(tmp_path: Path) -> None:
    real = git.RealGitBackend()
    working = git_fixtures.make_source_repo(tmp_path / "src", files={"README.md": "x\n"})
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    clone = tmp_path / "clone"
    real.clone_bare(f"file://{bare}", clone)

    class CountingBackend(git.RealGitBackend):
        def __init__(self) -> None:
            self.count = 0

        def resolve_ref(self, repo_dir: Path, ref: str) -> str:
            self.count += 1
            return super().resolve_ref(repo_dir, ref)

    backend = CountingBackend()
    git.set_backend(backend)
    try:
        lock._ref_cache.clear()
        first = lock._resolve_ref_cached(clone, "HEAD")
        again = lock._resolve_ref_cached(clone, "HEAD")
        third = lock._resolve_ref_cached(clone, "HEAD")
        assert first == again == third
        assert backend.count == 1, "repeated (repo, ref) must resolve once"

        # A different ref is a distinct key → resolves again.
        lock._resolve_ref_cached(clone, first)
        assert backend.count == 2

        # clear() (called at the start of each run) forces a fresh resolve so a
        # moved upstream is not served stale across runs.
        lock._ref_cache.clear()
        lock._resolve_ref_cached(clone, "HEAD")
        assert backend.count == 3
    finally:
        git.set_backend(real)
        lock._ref_cache.clear()
