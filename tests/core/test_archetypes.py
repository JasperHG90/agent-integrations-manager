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


def test_discover_ignores_noncanonical_paths(home: Path, tmp_path: Path) -> None:
    # Archetypes are explicit: an instruction file outside the canonical
    # instructions/ locations does NOT count unless linked explicitly.
    url = _repo_with_archetypes(
        tmp_path,
        {
            "bases/python/AGENTS.md": "# Python base\n",
            "team/CLAUDE.md": "# Team base\n",
            "AGENTS.md": "root, ignored\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    assert archetypes.list_archetypes() == []


def test_canonical_location_wins_over_aim_dir(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "# canonical lean\n",
            ".aim/instructions/lean/AGENTS.md": "# aim-dir lean\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    rows = {r.qualified_name: r for r in archetypes.list_archetypes()}
    assert rows["co/lean"].instruction_path == "instructions/lean/AGENTS.md"


def test_register_link_file_dir_and_root(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "bases/python/AGENTS.md": "# Python base\n",
            "bases/python/CLAUDE.md": "# claude python\n",
            "team/CLAUDE.md": "# Team base\n",
            "AGENTS.md": "# Root base\n",
        },
    )
    repos.add("co", url, allow_empty=True)

    # File link: name defaults to the parent directory.
    row = archetypes.register_link("co", "bases/python/AGENTS.md")
    assert row.qualified_name == "co/python"
    assert row.indexed_via == archetypes.VIA_LINK
    assert row.instruction_path == "bases/python/AGENTS.md"
    assert row.available == "AGENTS.md,CLAUDE.md"

    # Directory link: base file chosen by priority among files present.
    row = archetypes.register_link("co", "team", name="team")
    assert row.instruction_path == "team/CLAUDE.md"

    # Root file link: name defaults to the repo alias.
    row = archetypes.register_link("co", "AGENTS.md")
    assert row.qualified_name == "co/co"
    assert row.source_path == ""
    assert row.instruction_path == "AGENTS.md"

    names = {r.qualified_name for r in archetypes.list_archetypes()}
    assert names == {"co/python", "co/team", "co/co"}


def test_register_link_rejects_bad_paths(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(tmp_path, {"docs/notes.md": "# not standard\n"})
    repos.add("co", url, allow_empty=True)
    with pytest.raises(archetypes.ArchetypeLinkError):
        archetypes.register_link("co", "docs/notes.md")  # not a standard filename
    with pytest.raises(archetypes.ArchetypeLinkError):
        archetypes.register_link("co", "missing/AGENTS.md")  # not in the tree
    with pytest.raises(archetypes.ArchetypeLinkError):
        archetypes.register_link("co", "../escape/AGENTS.md")  # unsafe path


def test_register_link_survives_reindex(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "# canonical\n",
            "bases/python/AGENTS.md": "# linked\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    archetypes.register_link("co", "bases/python/AGENTS.md")

    archetypes.index_repo("co")  # rebuild — must not drop the explicit link

    rows = {r.qualified_name: r for r in archetypes.list_archetypes()}
    assert set(rows) == {"co/lean", "co/python"}
    assert rows["co/python"].indexed_via == archetypes.VIA_LINK
    assert rows["co/lean"].indexed_via == archetypes.VIA_DISCOVERED


def test_cli_archetype_use_with_explicit_path(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _repo_with_archetypes(tmp_path, {"bases/python/AGENTS.md": "# Python Base\nGo.\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)

    # A registered alias plus --path registers the link and selects it.
    res = _runner.invoke(cli.app, ["archetype", "use", "co", "--path", "bases/python/AGENTS.md"])
    assert res.exit_code == 0, res.output
    assert declarations.load(project_root).archetype.qualified_name == "co/python"

    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "Python Base" in (project_root / "AGENTS.md").read_text()


def test_dead_link_is_pruned_and_canonical_takes_the_name(home: Path, tmp_path: Path) -> None:
    """A link whose file vanished upstream is pruned on reindex, so it can never
    permanently shadow a valid canonical archetype of the same name."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"bases/python/AGENTS.md": "# linked\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "src.git")
    repos.add("co", f"file://{bare}", allow_empty=True)
    archetypes.register_link("co", "bases/python/AGENTS.md")

    # Upstream deletes the linked file and publishes a canonical archetype
    # under the same name (add_commit's `git add .` stages the deletion too).
    (working / "bases" / "python" / "AGENTS.md").unlink()
    git_fixtures.add_commit(
        working, {"instructions/python/AGENTS.md": "# canonical python\n"}, "canonicalize"
    )
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("co")

    rows = {r.qualified_name: r for r in archetypes.list_archetypes()}
    assert rows["co/python"].indexed_via == archetypes.VIA_DISCOVERED
    assert rows["co/python"].instruction_path == "instructions/python/AGENTS.md"


def test_register_link_refuses_canonical_and_discovered_names(home: Path, tmp_path: Path) -> None:
    """The canonical/discovered invariants live in the core API, not the CLI:
    any caller linking a canonical path or rebinding a discovered name fails."""
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "# canonical\n",
            "docs/lean/AGENTS.md": "# impostor\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    with pytest.raises(archetypes.ArchetypeLinkError, match="canonical instructions/ location"):
        archetypes.register_link("co", "instructions/lean/AGENTS.md")
    with pytest.raises(archetypes.ArchetypeLinkError, match="is a discovered archetype"):
        archetypes.register_link("co", "docs/lean/AGENTS.md")  # name 'lean' is taken
    # The discovered row is untouched.
    assert archetypes.index_row("co/lean").indexed_via == archetypes.VIA_DISCOVERED


def test_register_link_and_unlink_round_trip(home: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "# canonical\n",
            "bases/python/AGENTS.md": "# linked\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    archetypes.register_link("co", "bases/python/AGENTS.md")

    archetypes.unregister_link("co/python")
    assert {r.qualified_name for r in archetypes.list_archetypes()} == {"co/lean"}
    # Discovered rows are owned by discovery and cannot be unlinked.
    with pytest.raises(archetypes.ArchetypeLinkError):
        archetypes.unregister_link("co/lean")
    with pytest.raises(archetypes.ArchetypeNotIndexedError):
        archetypes.unregister_link("co/missing")


def test_flat_instructions_file_links_instead_of_erroring(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`instructions/AGENTS.md` with no per-archetype directory is not canonical
    (discovery finds nothing there) — it registers as an explicit link."""
    assert archetypes.canonical_archetype_name("instructions/AGENTS.md") is None
    assert archetypes.canonical_archetype_name("instructions/lean") == "lean"
    assert archetypes.canonical_archetype_name("instructions/lean/CLAUDE.md") == "lean"

    url = _repo_with_archetypes(tmp_path, {"instructions/AGENTS.md": "# Flat\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)

    res = _runner.invoke(cli.app, ["archetype", "use", "co", "--path", "instructions/AGENTS.md"])
    assert res.exit_code == 0, res.output
    assert declarations.load(project_root).archetype.qualified_name == "co/instructions"
    assert archetypes.index_row("co/instructions").indexed_via == archetypes.VIA_LINK


def test_canonical_path_naming_nonpriority_file_notes_the_base(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming CLAUDE.md inside a canonical dir still selects the priority base
    (AGENTS.md), but says so instead of silently substituting the file."""
    url = _repo_with_archetypes(
        tmp_path,
        {
            "instructions/lean/AGENTS.md": "# Lean agents\n",
            "instructions/lean/CLAUDE.md": "# Lean claude\n",
        },
    )
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)

    res = _runner.invoke(
        cli.app, ["archetype", "use", "co", "--path", "instructions/lean/CLAUDE.md"]
    )
    assert res.exit_code == 0, res.output
    assert declarations.load(project_root).archetype.qualified_name == "co/lean"
    assert "using its discovered base instructions/lean/AGENTS.md" in res.output


def test_cli_archetype_use_canonical_path_stays_discovered(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link that targets a canonical instructions/ location routes through the
    discovered flow — no sticky link row shadows the self-healing discovered one."""
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)

    res = _runner.invoke(
        cli.app, ["archetype", "use", "co", "--path", "instructions/lean/AGENTS.md"]
    )
    assert res.exit_code == 0, res.output
    assert declarations.load(project_root).archetype.qualified_name == "co/lean"
    assert archetypes.index_row("co/lean").indexed_via == archetypes.VIA_DISCOVERED


def test_cli_archetype_use_blob_url_registers_link(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL branch: a web link carrying an in-repo path registers the repo,
    writes a link row, and selects it (root files select as <alias>/<alias>)."""
    from aim.cli.commands import instructions as instructions_cmd

    url = _repo_with_archetypes(
        tmp_path,
        {
            "docs/AGENTS.md": "# Docs Base\n",
            "AGENTS.md": "# Root Base\n",
            "instructions/lean/AGENTS.md": "# Lean\n",
        },
    )
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    monkeypatch.chdir(project_root)
    # file:// URLs cannot carry a web subpath, so translate a fake https blob
    # URL to the local clone; everything downstream of parsing runs for real.
    fakes = {
        "https://git.example.com/org/co/blob/main/docs/AGENTS.md": "docs/AGENTS.md",
        "https://git.example.com/org/co/blob/main/AGENTS.md": "AGENTS.md",
        "https://git.example.com/org/co/tree/main/instructions/lean": "instructions/lean",
    }
    monkeypatch.setattr(instructions_cmd, "_parse_source_subpath", lambda u: fakes.get(u))
    monkeypatch.setattr(
        instructions_cmd,
        "_parse_source_url",
        lambda u: (url, "HEAD", None) if u in fakes else (u, None, None),
    )
    monkeypatch.setattr(
        "aim.cli._shared._parse_source_url",
        lambda u: (url, "HEAD", None) if u in fakes else (u, None, None),
    )

    # Subdir file link.
    res = _runner.invoke(
        cli.app,
        ["archetype", "use", "https://git.example.com/org/co/blob/main/docs/AGENTS.md", "--yes"],
    )
    assert res.exit_code == 0, res.output
    declared = declarations.load(project_root).archetype
    alias = declared.repo_alias
    assert declared.qualified_name == f"{alias}/docs"
    row = archetypes.index_row(declared.qualified_name)
    assert row.indexed_via == archetypes.VIA_LINK
    assert row.instruction_path == "docs/AGENTS.md"

    # Root file link: the name defaults to the repo alias.
    res = _runner.invoke(
        cli.app,
        ["archetype", "use", "https://git.example.com/org/co/blob/main/AGENTS.md", "--yes"],
    )
    assert res.exit_code == 0, res.output
    assert declarations.load(project_root).archetype.qualified_name == f"{alias}/{alias}"

    # Canonical tree URL: routed to the discovered row, not a link.
    res = _runner.invoke(
        cli.app,
        ["archetype", "use", "https://git.example.com/org/co/tree/main/instructions/lean", "--yes"],
    )
    assert res.exit_code == 0, res.output
    assert declarations.load(project_root).archetype.qualified_name == f"{alias}/lean"
    assert archetypes.index_row(f"{alias}/lean").indexed_via == archetypes.VIA_DISCOVERED

    # --path on a URL that already carries a subpath is ambiguous — rejected.
    res = _runner.invoke(
        cli.app,
        [
            "archetype",
            "use",
            "https://git.example.com/org/co/blob/main/docs/AGENTS.md",
            "--path",
            "AGENTS.md",
            "--yes",
        ],
    )
    assert res.exit_code != 0
    assert "conflicts" in res.output


def test_sync_recreates_link_row_for_locked_archetype(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # A teammate's machine has no link row (links are per-registration, not
    # discovered) — sync must recreate it from the lockfile.
    url = _repo_with_archetypes(tmp_path, {"bases/python/AGENTS.md": "# Python Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetypes.register_link("co", "bases/python/AGENTS.md")
    archetype_install.select(project_root, "co/python")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))

    # Simulate the other machine: drop the local index row.
    from sqlmodel import delete

    from aim.core import db
    from aim.core.models import ArchetypeIndex

    with db.session() as session:
        session.exec(delete(ArchetypeIndex))
        session.commit()
    with pytest.raises(archetypes.ArchetypeNotIndexedError):
        archetypes.index_row("co/python")

    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))

    row = archetypes.index_row("co/python")
    assert row.indexed_via == archetypes.VIA_LINK
    assert row.instruction_path == "bases/python/AGENTS.md"
    assert "Python Base" in (project_root / "AGENTS.md").read_text()


def test_select_lock_sync_renders_archetype(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(
        tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n\nBe terse.\n"}
    )
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))

    installed = archetype_install.select(project_root, "co/lean")
    assert installed.qualified_name == "co/lean"
    declared = declarations.load(project_root).archetype
    assert declared.qualified_name == "co/lean"

    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.archetype is not None
    assert m.archetype.qualified_name == "co/lean"

    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    agents = (project_root / "AGENTS.md").read_text()
    assert "Lean Base" in agents and "Be terse." in agents


def test_from_project_captures_archetype(home: Path, project_root: Path, tmp_path: Path) -> None:
    from aim.core import profiles

    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetype_install.select(project_root, "co/lean")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))

    snap = profiles.from_project("svc", project_root)

    assert snap.archetype.qualified_name == "co/lean"
    assert snap.archetype.sha  # frozen to the locked commit
    assert profiles.ProfileRepo(alias="co", url=url) in snap.repos
    # The exported TOML states the archetype explicitly.
    assert "[archetype]" in profiles.render_toml(snap)


def test_apply_profile_reconstructs_archetype(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import profiles

    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    sha = archetypes.index_row("co/lean").indexed_at_sha
    template = profiles.Profile(
        name="t",
        repos=[profiles.ProfileRepo(alias="co", url=url)],
        archetype=profiles.ProfileArchetype(qualified_name="co/lean", sha=sha),
    )

    profiles.apply_profile(template, project_root, strict_resolution=True)

    decl = declarations.load(project_root)
    assert decl.archetype.qualified_name == "co/lean"
    assert decl.archetype.pin == sha  # pinned to the template's frozen sha
    assert "Lean Base" in (project_root / "AGENTS.md").read_text()


def test_select_preserves_hand_edited_base(home: Path, project_root: Path, tmp_path: Path) -> None:
    """`archetype use` must not destroy user prose outside aim regions.

    Regression: the render was hardcoded force=True, so selecting an archetype
    silently deleted everything the user wrote outside the markers — violating
    the README's "anything you write outside them is preserved" contract.
    """
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))

    agents = project_root / "AGENTS.md"
    agents.write_text(agents.read_text() + "\n## My precious notes\n\nDo not lose me.\n")

    archetype_install.select(project_root, "co/lean")
    text = agents.read_text()
    assert "My precious notes" in text  # user prose preserved
    assert "Lean Base" not in text  # base swap withheld
    warnings = archetype_install.take_render_warnings()
    assert any("archetype base was NOT applied" in w for w in warnings)

    # A forced sync applies the base — explicitly, with a warning.
    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, force=True)))
    text = agents.read_text()
    assert "Lean Base" in text
    assert "My precious notes" not in text
    assert any("overwriting (--force)" in w for w in result.drift_warnings)


def test_select_applies_base_on_untouched_scaffold(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """When aim authored the current base (untouched scaffold), selecting an
    archetype swaps it in place without needing --force."""
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))

    archetype_install.select(project_root, "co/lean")
    assert "Lean Base" in (project_root / "AGENTS.md").read_text()


def test_clear_reverts_to_builtin_template(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetype_install.select(project_root, "co/lean")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))

    archetype_install.clear(project_root)
    assert declarations.load(project_root).archetype.is_builtin
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, force=True)))
    agents = (project_root / "AGENTS.md").read_text()
    assert "Lean Base" not in agents
    assert "Behavioral guidelines" in agents  # the built-in default template


def test_sync_gates_archetype_content(home: Path, project_root: Path, tmp_path: Path) -> None:
    """The archetype gate runs at the render chokepoint, not just at select:
    a repo blocked AFTER selection must not keep re-rendering into AGENTS.md
    (regression: lock->sync re-rendered blocked/non-allow-listed archetype
    content with no policy or risk check)."""
    url = _repo_with_archetypes(tmp_path, {"instructions/lean/AGENTS.md": "# Lean Base\n"})
    repos.add("co", url, allow_empty=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    archetype_install.select(project_root, "co/lean")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))

    section = policy.to_mapping(policy.Policy(name="org", blocked_repos=["co"]))
    section["scope"] = "local"
    policy.set_project_policy(project_root, section)

    with pytest.raises(Exception, match="blocked by policy"):
        asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, force=True)))


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
    declared = declarations.load(project_root).archetype
    assert declared.qualified_name == "co/lean"

    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "Lean Base" in (project_root / "AGENTS.md").read_text()

    res = _runner.invoke(cli.app, ["archetype", "clear"])
    assert res.exit_code == 0, res.output
    # Clearing reverts to the built-in instruction_template (no archetype).
    assert declarations.load(project_root).archetype.is_builtin
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
    declared = declarations.load(project_root).archetype
    assert declared.qualified_name == "co/lean"


def test_assert_archetype_allowed_permits_builtin_and_empty_list() -> None:
    pol = policy.Policy(name="p", allowed_archetypes=["a/b"])
    policy.assert_archetype_allowed(pol, "a/b")
    policy.assert_archetype_allowed(pol, None)  # built-in always allowed
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_archetype_allowed(pol, "a/other")
    policy.assert_archetype_allowed(policy.Policy(), "anything")  # empty = all allowed
