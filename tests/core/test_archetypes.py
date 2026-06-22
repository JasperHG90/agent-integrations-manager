"""Tests for project-instruction archetype discovery, selection, lock, and render."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aim import cli
from aim.core import (
    archetype_install,
    archetypes,
    declarations,
    lock,
    manifest,
    policy,
    repos,
    sync,
)
from aim.core import init as init_mod
from tests.fixtures import git_fixtures

_runner = CliRunner()


def _repo_with_archetypes(tmp_path: Path, files: dict[str, str], name: str = "src") -> str:
    working = git_fixtures.make_source_repo(tmp_path / name, files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / f"{name}.git")
    return f"file://{bare}"


def test_discover_indexes_instruction_dirs(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "---\ntitle: Lean\ndescription: terse\n---\n# Lean\n",
            "instructions/lean/CLAUDE.md": "# claude lean\n",
            "instructions/verbose/CLAUDE.md": "# verbose\n",
            "AGENTS.md": "root file, must be ignored\n",  # root is never an archetype
            "README.md": "noise\n",
        },
    )
    repos.add("co", url, allow_empty=True)

    rows = {r.qualified_name: r for r in archetypes.list_archetypes()}
    assert set(rows) == {"co/lean", "co/verbose"}
    assert rows["co/lean"].instruction_path == "instructions/lean/AGENTS.md"
    assert rows["co/lean"].available == "AGENTS.md,CLAUDE.md"
    assert rows["co/lean"].title == "Lean"
    # An archetype with only CLAUDE.md uses it as the base.
    assert rows["co/verbose"].instruction_path == "instructions/verbose/CLAUDE.md"
    assert rows["co/verbose"].available == "CLAUDE.md"


def test_discover_picks_up_noncanonical_subdirs(home: Path, tmp_path: Path) -> None:
    # Any non-root directory with an instruction file is an archetype, so a base
    # can be authored anywhere in a repo (and then shared + locked).
    url = _repo_with_archetypes(
        tmp_path,
        {
            "bases/python/AGENTS.md": "# Python base\n",
            "team/CLAUDE.md": "# Team base\n",
            "AGENTS.md": "root, ignored\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    rows = {r.qualified_name: r for r in archetypes.list_archetypes()}
    assert "co/python" in rows
    assert rows["co/python"].instruction_path == "bases/python/AGENTS.md"
    assert "co/team" in rows
    assert rows["co/team"].instruction_path == "team/CLAUDE.md"


def test_canonical_location_wins_over_arbitrary(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "# canonical lean\n",
            "elsewhere/lean/AGENTS.md": "# other lean\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    rows = {r.qualified_name: r for r in archetypes.list_archetypes()}
    assert rows["co/lean"].instruction_path == "instructions/lean/AGENTS.md"


def test_select_lock_sync_renders_archetype(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n\nBe terse.\n"}
    )
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))

    installed = archetype_install.select(project_root, "co/lean")
    assert installed.qualified_name == "co/lean"
    declared = declarations.load(project_root).instruction_archetype
    assert declared is not None and declared.qualified_name == "co/lean"

    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.instruction_archetype is not None
    assert m.instruction_archetype.qualified_name == "co/lean"

    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    agents = (project_root / "AGENTS.md").read_text()
    assert "Lean Base" in agents and "Be terse." in agents


def test_clear_reverts_to_builtin_template(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetype_install.select(project_root, "co/lean")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))

    archetype_install.clear(project_root)
    assert declarations.load(project_root).instruction_archetype is None
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, force=True)))
    agents = (project_root / "AGENTS.md").read_text()
    assert "Lean Base" not in agents
    assert "Behavioral guidelines" in agents  # the built-in default template


def test_policy_allow_list_blocks_unlisted_archetype(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/ok/AGENTS.md": "# ok\n",
            "instructions/nope/AGENTS.md": "# nope\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    section = policy.to_mapping(policy.Policy(name="org", allowed_archetypes=["co/ok"]))
    section["scope"] = "local"
    policy.set_project_policy(project_root, section)

    archetype_install.select(project_root, "co/ok")  # allowed
    with pytest.raises(policy.PolicyViolationError):
        archetype_install.select(project_root, "co/nope")  # not in allow-list


def test_frontmatter_is_not_rendered_into_agents_md(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "---\ntitle: Lean\ndescription: terse\n---\n# Real Body\nGo.\n"
        },
    )
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetype_install.select(project_root, "co/lean")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    agents = (project_root / "AGENTS.md").read_text()
    assert "# Real Body" in agents
    assert "title: Lean" not in agents  # frontmatter stripped
    assert "---" not in agents.splitlines()[0]


def test_lock_is_unchanged_on_second_run_after_select(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # select and lock must resolve the same SHA, so a re-lock detects no change
    # (regression: select used resolve_install_version, lock used the branch tip).
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetype_install.select(project_root, "co/lean")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    second = asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    assert second.unchanged is True


def test_cli_archetype_use_clear_round_trip(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _repo_with_archetypes(
        tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n\nBe terse.\n"}
    )
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)

    res = _runner.invoke(cli.app, ["archetype", "use", "co/lean"])
    assert res.exit_code == 0, res.output
    declared = declarations.load(project_root).instruction_archetype
    assert declared is not None and declared.qualified_name == "co/lean"

    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "Lean Base" in (project_root / "AGENTS.md").read_text()

    res = _runner.invoke(cli.app, ["archetype", "clear"])
    assert res.exit_code == 0, res.output
    # Clearing reverts to the built-in instruction_template (no archetype).
    assert declarations.load(project_root).instruction_archetype is None
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, force=True)))
    assert "Lean Base" not in (project_root / "AGENTS.md").read_text()


def test_cli_instructions_alias_still_selects_archetype(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)

    # The hidden back-compat `aim instructions` alias dispatches the same command.
    res = _runner.invoke(cli.app, ["instructions", "use", "co/lean"])
    assert res.exit_code == 0, res.output
    declared = declarations.load(project_root).instruction_archetype
    assert declared is not None and declared.qualified_name == "co/lean"


def test_assert_archetype_allowed_permits_builtin_and_empty_list() -> None:
    pol = policy.Policy(name="p", allowed_archetypes=["a/b"])
    policy.assert_archetype_allowed(pol, "a/b")
    policy.assert_archetype_allowed(pol, None)  # built-in always allowed
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_archetype_allowed(pol, "a/other")
    policy.assert_archetype_allowed(policy.Policy(), "anything")  # empty = all allowed
