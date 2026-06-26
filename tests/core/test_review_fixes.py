"""Tests covering the fixes triggered by the post-phase-5 adversarial review.

Each test is named after the finding it addresses (see the review thread).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from aim.core import init as init_mod
from aim.core import install, manifest, paths, repos, rule_install
from aim.core import sync as sync_mod
from aim.core.install import _SNAPSHOT_SENTINEL
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run
from tests.fixtures import git_fixtures


def _build_repo(tmp_path: Path, files: dict[str, str], tag: str | None = None) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    if tag is not None:
        git_fixtures.add_tag(working, tag)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


# ---------- #1: tag-after-edit must not be attached ----------


def test_tag_not_attached_when_skill_edited_after_tag(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# v1\n"}
    )
    git_fixtures.add_tag(working, "v1.0.0")
    # Edit the skill AFTER the tag — the tag should NOT label this install.
    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v1+1\n"}, "post-tag edit")
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/foo")
    assert installed.current.tag is None
    assert "+" not in installed.current.identifier()


def test_tag_attached_when_at_tag(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# v1\n"}
    )
    git_fixtures.add_tag(working, "v1.0.0")
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/foo")
    assert installed.current.tag == "v1.0.0"


# ---------- #2: update refuses to overwrite local edits ----------


def test_update_refuses_when_target_has_local_edits(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    target = project_root / ".claude" / "skills" / "foo"
    (target / "SKILL.md").write_text("# locally edited\n")

    git_fixtures.add_commit(working, {"skills/foo/extra.md": "x\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")

    with pytest.raises(install.LocalEditsError):
        install.update(project_root, "a/foo")


def test_update_with_force_overrides_local_edits(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    target = project_root / ".claude" / "skills" / "foo"
    (target / "SKILL.md").write_text("# locally edited\n")

    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")

    install.update(project_root, "a/foo", force=True)
    assert (target / "SKILL.md").read_text() == "# v2\n"


# ---------- #3: in-region drift warning on re-init ----------


def test_sync_warns_on_in_region_drift(home: Path, project_root: Path, tmp_path: Path) -> None:
    from aim.core import layout_profiles

    _, bare = _build_repo(tmp_path, {"rules/focus.md": "Focus.\n", "README.md": "x\n"})
    repos.add("a", f"file://{bare}")
    # Use inline rules mode so the rule body lives inside the managed rules region.
    layout_profiles.save_project_profile(
        project_root,
        layout_profiles.LayoutProfile(
            name="inline",
            skills_dir=".claude/skills",
            rules_dir=".claude/rules",
            agents_dir=".claude/agents",
            agents_md="AGENTS.md",
            mcp_json=".mcp.json",
            rules_mode="inline",
        ),
    )
    init_mod.run(init_mod.InitOptions(project_root=project_root, layout_profile="inline"))
    rule_install.install(project_root, "a/focus")
    asyncio.run(lock_run(LockOptions(project_root=project_root)))
    asyncio.run(
        sync_mod.run(sync_mod.SyncOptions(project_root=project_root, layout_profile="inline"))
    )
    agents = project_root / "AGENTS.md"

    text = agents.read_text()
    edited = text.replace("Focus.", "Focus. (edited inside marker)")
    agents.write_text(edited)

    result = asyncio.run(
        sync_mod.run(sync_mod.SyncOptions(project_root=project_root, layout_profile="inline"))
    )
    assert any("rules" in w and "edited" in w for w in result.drift_warnings)


# ---------- #5: snapshot sentinel survives partial extraction ----------


def test_partial_snapshot_is_re_extracted(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _build_repo(
        tmp_path,
        {
            "skills/foo/SKILL.md": "# foo\n",
            "skills/foo/extra.md": "auxiliary\n",
        },
    )
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/foo")

    snap = paths.snapshots_cache_dir() / "a" / installed.current.sha / "foo"
    assert (snap / _SNAPSHOT_SENTINEL).exists()

    # Simulate a partial extraction: nuke the sentinel and one of the files.
    (snap / _SNAPSHOT_SENTINEL).unlink()
    (snap / "extra.md").unlink()

    # Reinstall (delete + install) should re-materialise the snapshot fully.
    install.delete(project_root, "a/foo")
    install.install(project_root, "a/foo")
    assert (snap / _SNAPSHOT_SENTINEL).exists()
    assert (snap / "extra.md").exists()


# ---------- #6: archive failure surfaces the git error, not a tar error ----------


def test_archive_bad_sha_surfaces_git_error(home: Path, tmp_path: Path) -> None:
    from aim.core import git as git_mod

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    dest = tmp_path / "out"
    with pytest.raises(git_mod.GitError) as excinfo:
        git_mod.get_backend().archive(repos.clone_dir("a"), "0" * 40, "skills/foo", dest)
    assert "git archive failed" in str(excinfo.value)


# ---------- #7: repos.add rolls back on indexing failure ----------


def test_repos_add_rolls_back_on_indexing_failure(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})

    from aim.core import skills as skills_mod

    def boom(_alias: str) -> None:
        raise RuntimeError("simulated indexing failure")

    monkeypatch.setattr(skills_mod, "index_repo", boom)
    with pytest.raises(RuntimeError, match="simulated indexing failure"):
        repos.add("a", f"file://{bare}")
    with pytest.raises(repos.RepoNotFoundError):
        repos.get("a")
    assert not repos.clone_dir("a").exists()


# ---------- #4: rollback honors repo_alias from manifest (survives rename) ----------


def test_rollback_works_after_repo_rename(home: Path, project_root: Path, tmp_path: Path) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")
    install.update(project_root, "a/foo")

    # Rename the repo locally. The committed lockfile is identity-keyed (by the
    # repo's URL, which the rename does not change), so on load it transparently
    # resolves to the repo's CURRENT local alias — rollback works under it.
    repos.rename("a", "renamed")

    rolled = install.rollback(project_root, "renamed/foo")
    target = project_root / ".claude" / "skills" / "foo"
    assert rolled.current.identifier()  # didn't crash
    assert (target / "SKILL.md").read_text() == "# v1\n"


# ---------- #9: refresh raises when default_ref disappears ----------


def test_refresh_raises_when_ref_disappears(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    repos.add("a", f"file://{bare}", default_ref="refs/heads/main")
    # Wipe the upstream branch.
    import subprocess

    subprocess.run(
        ["git", "-C", str(bare), "branch", "-D", "main"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(repos.RefDisappearedError):
        repos.refresh("a")


# ---------- #18: source_path at repo root end-to-end ----------


def test_install_root_level_skill(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _build_repo(
        tmp_path,
        {"rootskill/SKILL.md": "# root\n", "rootskill/extra.md": "x\n", "README.md": "y\n"},
    )
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/rootskill")
    target = project_root / installed.target_dir
    assert (target / "SKILL.md").read_text() == "# root\n"
    assert (target / "extra.md").read_text() == "x\n"


# ---------- #19: multi-skill in one repo ----------


def test_multi_skill_install_and_update_independence(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _build_repo(
        tmp_path,
        {
            "skills/foo/SKILL.md": "# foo v1\n",
            "skills/bar/SKILL.md": "# bar v1\n",
        },
    )
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    install.install(project_root, "a/bar")

    foo_sha = manifest.load(project_root).skills[0].current.sha
    bar_sha = manifest.load(project_root).skills[1].current.sha

    # Touch only bar.
    git_fixtures.add_commit(working, {"skills/bar/SKILL.md": "# bar v2\n"}, "bar v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")

    install.update(project_root, "a/foo")
    install.update(project_root, "a/bar")
    m = manifest.load(project_root)
    foo_after = next(s for s in m.skills if s.qualified_name == "a/foo")
    bar_after = next(s for s in m.skills if s.qualified_name == "a/bar")
    assert foo_after.current.sha == foo_sha  # untouched, history empty
    assert foo_after.history == []
    assert bar_after.current.sha != bar_sha


# ---------- snapshot remains valid across rename of repo ----------


def test_rollback_unavailable_when_both_gone(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")
    install.update(project_root, "a/foo")

    # Wipe snapshot AND upstream cache. v1 is now unrecoverable.
    snap_root = paths.snapshots_cache_dir() / "a"
    shutil.rmtree(snap_root)
    shutil.rmtree(repos.clone_dir("a"))
    shutil.rmtree(bare)

    with pytest.raises(install.RollbackUnavailableError):
        install.rollback(project_root, "a/foo")
