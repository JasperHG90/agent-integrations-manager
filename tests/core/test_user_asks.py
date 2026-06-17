"""Tests for the user-requested round of changes:

1. Symlink union on re-init (existing symlinks don't silently disappear).
2. `rules.install_to_project` adds rule to manifest + re-renders AGENTS.md.
3. Per-project `agent_dialect` round-trips through the manifest.
4. `re.fullmatch` rejects trailing-newline names (adversarial finding #15).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aim.core import declarations, manifest, repos, rules
from aim.core import init as init_mod
from aim.core import sync as sync_mod
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run


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


def test_rule_install_adds_to_manifest_and_renders(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    rules.add("be-concise", "Be concise.")

    rules.install_to_project(project_root, "be-concise")
    _lock_and_sync(project_root)
    m = manifest.load(project_root)
    assert "be-concise" in m.rules
    agents_md = (project_root / "AGENTS.md").read_text()
    assert "Be concise." in agents_md


def test_rule_install_preserves_symlinks(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md",)))
    rules.add("focus", "Focus.")
    rules.install_to_project(project_root, "focus")
    _lock_and_sync(project_root)

    assert (project_root / "CLAUDE.md").exists()
    assert "Focus." in (project_root / "CLAUDE.md").read_text()


def test_rule_install_unknown_errors(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    with pytest.raises(rules.RuleNotFoundError):
        rules.install_to_project(project_root, "ghost")


# ---------- 3. Agent dialect ----------


def test_agent_dialect_stored_in_manifest(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, agent_dialect="claude"))
    _lock_and_sync(project_root)
    m = manifest.load(project_root)
    assert m.agent_dialect == "claude"


def test_agent_dialect_preserved_on_reinit_when_none_passed(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, agent_dialect="claude"))
    _lock_and_sync(project_root)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    _lock_and_sync(project_root)
    m = manifest.load(project_root)
    assert m.agent_dialect == "claude"


# ---------- 4. fullmatch trailing-newline ----------


def test_rule_name_rejects_trailing_newline(home: Path) -> None:
    with pytest.raises(rules.RuleNameError):
        rules.add("ok\n", "body")


def test_repo_alias_rejects_trailing_newline(home: Path, tmp_path: Path) -> None:
    from tests.fixtures import git_fixtures

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    with pytest.raises(repos.RepoAliasError):
        repos.add("anth\n", f"file://{bare}")
