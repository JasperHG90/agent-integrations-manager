from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aim.core import declarations, init, install, lock, manifest, prune, repos
from aim.core.models import (
    DeclaredMcpServer,
    DeclaredSkill,
    InstalledMcpServer,
    McpClaudeEntry,
    McpServerVersion,
)
from tests.fixtures import git_fixtures


def _skill_repo(tmp_path: Path) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n\nFoo skill.\n"},
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def _lock(project_root: Path) -> None:
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))


def _install_skill_for_drift(project_root: Path, tmp_path: Path) -> str:
    """Install a skill, then remove it from aim.toml to create drift.

    Returns the skill's target_dir.
    """
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    _lock(project_root)
    # Remove the declaration only — leaves lockfile + disk intact (drift).
    declarations._remove_skill(project_root, "a/foo")
    return ".claude/skills/foo"


# ---------- drift detection: skills ----------


def test_prune_dry_run_previews_deletions(home: Path, project_root: Path, tmp_path: Path) -> None:
    target = _install_skill_for_drift(project_root, tmp_path)

    result = prune.plan(prune.PruneOptions(project_root=project_root, dry_run=True))
    removed_paths = {i.path for i in result.removed if i.action == "would-remove"}
    assert target in removed_paths
    # Nothing actually removed in plan mode.
    assert (project_root / target).exists()


def test_prune_removes_drifted_skill(home: Path, project_root: Path, tmp_path: Path) -> None:
    target = _install_skill_for_drift(project_root, tmp_path)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert target in removed_paths
    assert not (project_root / target).exists()
    # Lockfile entry gone.
    m = manifest.load(project_root)
    assert not any(s.target_dir == target for s in m.skills)


def test_prune_leaves_external_files_alone(home: Path, project_root: Path, tmp_path: Path) -> None:
    """Files on disk not in the lockfile (e.g. Terraform plugins) are never touched."""
    _install_skill_for_drift(project_root, tmp_path)

    # An external tool installed a skill not tracked by aim.
    external = project_root / ".claude" / "skills" / "terraform"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("terraform\n")

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    # The external skill must not appear in the removal list.
    assert not any(i.path == ".claude/skills/terraform" for i in result.removed)
    # And must still exist on disk.
    assert external.exists()
    assert (external / "SKILL.md").read_text() == "terraform\n"


# ---------- drift detection: rules ----------


def test_prune_removes_drifted_rule(home: Path, project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    _lock(project_root)

    # Add "managed" to the lockfile + aim.toml, then remove from aim.toml (drift).
    m = manifest.load(project_root)
    m.rules = ["managed"]
    manifest.save(project_root, m)
    decl = declarations.load(project_root)
    decl.rules = ["managed"]
    declarations.save(project_root, decl)
    decl.rules = []
    declarations.save(project_root, decl)

    rules_dir = project_root / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "managed.md").write_text("managed\n")

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert ".claude/rules/managed.md" in removed_paths
    assert not (rules_dir / "managed.md").exists()
    m = manifest.load(project_root)
    assert "managed" not in m.rules


def test_prune_inline_rule_no_file_deletion(home: Path, project_root: Path) -> None:
    """In inline rules mode (Gemini), removing a rule deletes no on-disk file."""
    init.run(init.InitOptions(project_root=project_root, layout_profile="gemini"))
    _lock(project_root)

    # Add a rule to the lockfile + aim.toml, then remove from aim.toml (drift).
    m = manifest.load(project_root)
    m.rules = ["myrule"]
    manifest.save(project_root, m)
    decl = declarations.load(project_root)
    decl.rules = ["myrule"]
    declarations.save(project_root, decl)
    decl.rules = []
    declarations.save(project_root, decl)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    # No file deletion occurred; only the lockfile entry.
    actions = {i.action for i in result.removed}
    assert "removed-stale-entry" in actions
    assert "removed" not in actions
    m = manifest.load(project_root)
    assert "myrule" not in m.rules
    # Warning about stale AGENTS.md.
    assert any("AGENTS.md" in w for w in result.warnings)


# ---------- drift detection: MCP ----------


def _install_mcp_for_drift(project_root: Path, alias: str = "managed") -> None:
    """Write an MCP entry directly into the lockfile + .mcp.json + aim.toml,
    then remove the aim.toml declaration to create drift."""
    init.run(init.InitOptions(project_root=project_root))
    _lock(project_root)
    # Add to lockfile.
    m = manifest.load(project_root)
    entry = McpClaudeEntry(type="stdio", command="managed", args=[], env={}, url=None, headers={})
    version = McpServerVersion(
        definition_hash="deadbeef",
        registry_version="1.0.0",
        installed_at=datetime.now(UTC),
        entry=entry,
        overrides=None,
    )
    installed = InstalledMcpServer(
        alias=alias,
        registry_name="managed",
        entry=entry,
        entry_hash="abc",
        current=version,
        history=[],
        overrides=None,
    )
    m.mcp_servers.append(installed)
    manifest.save(project_root, m)
    # Add to .mcp.json.
    mcp_path = project_root / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {alias: {"command": "managed"}}}))
    # Add declaration to aim.toml.
    decl = declarations.load(project_root)
    decl.mcp_servers.append(
        DeclaredMcpServer(
            alias=alias, registry_name="managed", preferred_transport="stdio", overrides={}
        )
    )
    declarations.save(project_root, decl)
    # Now remove the declaration to create drift.
    decl.mcp_servers = []
    declarations.save(project_root, decl)


def test_prune_removes_drifted_mcp_alias(home: Path, project_root: Path) -> None:
    _install_mcp_for_drift(project_root, alias="managed")

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    removed_aliases = {i.path for i in result.removed if i.kind == "mcp"}
    assert "managed" in removed_aliases
    m = manifest.load(project_root)
    assert not any(mc.alias == "managed" for mc in m.mcp_servers)
    data = json.loads((project_root / ".mcp.json").read_text())
    assert "managed" not in data.get("mcpServers", {})


def test_prune_leaves_external_mcp_aliases_alone(home: Path, project_root: Path) -> None:
    """MCP aliases in .mcp.json not in the lockfile are left alone."""
    init.run(init.InitOptions(project_root=project_root))
    (project_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"terraform-bot": {"command": "terraform"}}})
    )
    _lock(project_root)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    assert not any(i.path == "terraform-bot" for i in result.removed)
    data = json.loads((project_root / ".mcp.json").read_text())
    assert "terraform-bot" in data.get("mcpServers", {})


# ---------- drift detection: symlinks ----------


def test_prune_symlink_drift(home: Path, project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))  # claude profile, symlinks=["CLAUDE.md"]
    _lock(project_root)
    # init declares the symlink but doesn't create it on disk; do that manually.
    claude_link = project_root / "CLAUDE.md"
    claude_link.symlink_to("aim/CLAUDE.md")
    assert claude_link.is_symlink()

    # Remove symlinks declaration from aim.toml to create drift.
    decl = declarations.load(project_root)
    decl.symlinks = []
    declarations.save(project_root, decl)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    removed = {i.path for i in result.removed if i.kind == "symlink"}
    assert "CLAUDE.md" in removed
    m = manifest.load(project_root)
    assert "CLAUDE.md" not in m.symlinks


# ---------- .aimignore / --exclude on drift candidates ----------


def test_prune_aimignore_protects_drifted_skill(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    target = _install_skill_for_drift(project_root, tmp_path)
    (project_root / ".aimignore").write_text(f"{target}\n")

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    # Should be skipped, not removed.
    assert any(i.path == target and i.action == "skipped" for i in result.removed)
    assert (project_root / target).exists()


def test_prune_cli_exclude_option(home: Path, project_root: Path, tmp_path: Path) -> None:
    target = _install_skill_for_drift(project_root, tmp_path)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True, excludes=[target]))
    assert not any(i.path == target and i.action == "removed" for i in result.removed)
    assert (project_root / target).exists()


def test_prune_aimignore_protects_drifted_mcp_alias(home: Path, project_root: Path) -> None:
    _install_mcp_for_drift(project_root, alias="local-db")
    (project_root / ".aimignore").write_text("mcp:local-*\n")

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    assert not any(i.path == "local-db" and i.action == "removed" for i in result.removed)
    data = json.loads((project_root / ".mcp.json").read_text())
    assert "local-db" in data.get("mcpServers", {})


def test_prune_aimignore_obsolete_pattern_warning(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _install_skill_for_drift(project_root, tmp_path)
    # A pattern targeting a path that's not a drift candidate.
    (project_root / ".aimignore").write_text(".claude/skills/terraform/*\n")

    result = prune.plan(prune.PruneOptions(project_root=project_root))
    assert any("terraform" in w for w in result.warnings)


# ---------- error / edge cases ----------


def test_prune_missing_aim_toml(home: Path, project_root: Path, tmp_path: Path) -> None:
    _install_skill_for_drift(project_root, tmp_path)
    # Delete aim.toml entirely.
    (project_root / "aim.toml").unlink()

    result = prune.plan(prune.PruneOptions(project_root=project_root))
    assert any("no aim.toml" in w for w in result.warnings)
    # Nothing removed.
    assert not any(i.action == "would-remove" for i in result.removed)


def test_prune_missing_target_on_disk(home: Path, project_root: Path, tmp_path: Path) -> None:
    target = _install_skill_for_drift(project_root, tmp_path)
    # Delete the on-disk skill directory but leave the lockfile entry.
    import shutil

    shutil.rmtree(project_root / target)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    actions = {i.action for i in result.removed if i.path == target}
    assert "removed-stale-entry" in actions
    m = manifest.load(project_root)
    assert not any(s.target_dir == target for s in m.skills)


def test_prune_layout_profile_mismatch(home: Path, project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root, layout_profile="claude"))
    _lock(project_root)
    # Switch aim.toml to gemini without syncing.
    decl = declarations.load(project_root)
    decl.layout_profile = "gemini"
    declarations.save(project_root, decl)

    with pytest.raises(prune.PruneError, match="layout_profile"):
        prune.plan(prune.PruneOptions(project_root=project_root))


def test_prune_apply_revalidates_state(home: Path, project_root: Path, tmp_path: Path) -> None:
    """If a drifted skill is re-added to aim.toml between plan and apply,
    apply must skip it."""
    target = _install_skill_for_drift(project_root, tmp_path)

    plan_result = prune.plan(prune.PruneOptions(project_root=project_root))
    assert any(i.path == target and i.action == "would-remove" for i in plan_result.removed)

    # Re-add the declaration.
    decl = declarations.load(project_root)
    decl.skills.append(
        DeclaredSkill(
            qualified_name="a/foo",
            repo_alias="a",
            source_path="skills/foo",
            target_dir=target,
        )
    )
    declarations.save(project_root, decl)

    apply_result = prune.apply(
        prune.PruneOptions(project_root=project_root, force=True), plan_result
    )
    # Skill should not be removed.
    assert not any(i.path == target and i.action == "removed" for i in apply_result.removed)
    assert (project_root / target).exists()


def test_prune_apply_stale_plan_noop(home: Path, project_root: Path, tmp_path: Path) -> None:
    """If all plan items are no longer drift candidates at apply time, apply does nothing."""
    target = _install_skill_for_drift(project_root, tmp_path)
    plan_result = prune.plan(prune.PruneOptions(project_root=project_root))

    # Re-add the declaration so drift disappears.
    decl = declarations.load(project_root)
    decl.skills.append(
        DeclaredSkill(
            qualified_name="a/foo",
            repo_alias="a",
            source_path="skills/foo",
            target_dir=target,
        )
    )
    declarations.save(project_root, decl)

    apply_result = prune.apply(
        prune.PruneOptions(project_root=project_root, force=True), plan_result
    )
    assert any("stale" in w.lower() for w in apply_result.warnings)
    assert not apply_result.removed


def test_prune_no_drift_returns_empty(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    _lock(project_root)

    result = prune.plan(prune.PruneOptions(project_root=project_root))
    assert not any(i.action == "would-remove" for i in result.removed)


def test_prune_force_applies_without_prompt(home: Path, project_root: Path, tmp_path: Path) -> None:
    """run() with force=True applies the plan immediately (no prompt)."""
    target = _install_skill_for_drift(project_root, tmp_path)

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    assert any(i.path == target and i.action == "removed" for i in result.removed)
    assert not (project_root / target).exists()


def test_prune_malformed_aimignore_is_skipped(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """Binary .aimignore content is ignored, not crashed on."""
    target = _install_skill_for_drift(project_root, tmp_path)
    (project_root / ".aimignore").write_bytes(b"\xff\xfe")

    result = prune.run(prune.PruneOptions(project_root=project_root, force=True))
    # Skill still pruned (ignore file ignored).
    assert any(i.path == target and i.action == "removed" for i in result.removed)
