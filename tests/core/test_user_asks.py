"""Tests for the user-requested round of changes:

1. Symlink union on re-init (existing symlinks don't silently disappear).
2. `rules.install_to_project` adds rule to manifest + re-renders AGENTS.md.
3. `re.fullmatch` rejects trailing-newline names (adversarial finding #15).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aim.core import declarations, manifest, repos, rule_install
from aim.core import init as init_mod
from aim.core import sync as sync_mod
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run
from tests.fixtures import git_fixtures


def _repo_with_rule(tmp_path: Path, name: str = "be-concise", body: str = "Be concise.") -> str:
    working = git_fixtures.make_source_repo(
        tmp_path / f"src-{name}", files={f"rules/{name}.md": f"{body}\n", "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / f"bare-{name}.git")
    repos.add("anth", f"file://{bare}")
    return f"anth/{name}"


def _lock_and_sync(project_root: Path) -> None:
    asyncio.run(lock_run(LockOptions(project_root=project_root)))
    asyncio.run(sync_mod.run(sync_mod.SyncOptions(project_root=project_root)))


# ---------- 1. Symlink union ----------


def test_re_init_preserves_existing_symlinks_when_none_specified(
    home: Path, project_root: Path
) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md",)))
    _lock_and_sync(project_root)
    assert (project_root / "CLAUDE.md").exists()

    # Re-init with no symlink flag — CLAUDE.md must still be declared and rendered.
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    _lock_and_sync(project_root)
    assert (project_root / "CLAUDE.md").exists()
    m = manifest.load(project_root)
    assert "CLAUDE.md" in m.symlinks


def test_re_init_unions_new_with_existing(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md",)))
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("GEMINI.md",)))
    _lock_and_sync(project_root)
    decl = declarations.load(project_root)
    m = manifest.load(project_root)
    assert "CLAUDE.md" in decl.symlinks
    assert "GEMINI.md" in decl.symlinks
    assert "CLAUDE.md" in m.symlinks
    assert "GEMINI.md" in m.symlinks


def test_re_init_clear_symlinks_wipes(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md",)))
    _lock_and_sync(project_root)
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=(), clear_symlinks=True))
    _lock_and_sync(project_root)
    decl = declarations.load(project_root)
    m = manifest.load(project_root)
    assert decl.symlinks == []
    assert m.symlinks == []


# ---------- 2. Rule install flow ----------


def _save_inline_profile(project_root: Path) -> None:
    from aim.core import layout_profiles

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


def test_rule_install_adds_to_manifest_and_renders(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _save_inline_profile(project_root)
    init_mod.run(init_mod.InitOptions(project_root=project_root, layout_profile="inline"))
    qn = _repo_with_rule(tmp_path)

    rule_install.install(project_root, qn)
    _lock_and_sync(project_root)
    m = manifest.load(project_root)
    assert [r.qualified_name for r in m.rules] == [qn]
    agents_md = (project_root / "AGENTS.md").read_text()
    assert "Be concise." in agents_md


def test_rule_install_preserves_symlinks(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _save_inline_profile(project_root)
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root, layout_profile="inline", symlinks=("CLAUDE.md",)
        )
    )
    qn = _repo_with_rule(tmp_path, name="focus", body="Focus.")
    rule_install.install(project_root, qn)
    _lock_and_sync(project_root)

    assert (project_root / "CLAUDE.md").exists()
    assert "Focus." in (project_root / "CLAUDE.md").read_text()


def test_rule_install_unknown_errors(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    with pytest.raises(rule_install.RuleNotIndexedError):
        rule_install.install(project_root, "ghost/missing")


# ---------- 3. fullmatch trailing-newline ----------


def test_repo_alias_rejects_trailing_newline(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    with pytest.raises(repos.RepoAliasError):
        repos.add("anth\n", f"file://{bare}")
