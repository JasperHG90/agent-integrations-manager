from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aim.core import init, install, lock, prune, repos, rules
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


def test_prune_dry_run_previews_deletions(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    _lock(project_root)

    unmanaged = project_root / ".claude" / "skills" / "orphan"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("orphan\n")

    result = prune.run(prune.PruneOptions(project_root=project_root, dry_run=True))
    removed_paths = {i.path for i in result.removed if i.action == "would-remove"}
    assert ".claude/skills/orphan" in removed_paths
    assert unmanaged.exists()


def test_prune_removes_unmanaged_skill(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    _lock(project_root)

    unmanaged = project_root / ".claude" / "skills" / "orphan"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("orphan\n")

    result = prune.run(prune.PruneOptions(project_root=project_root))
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert ".claude/skills/orphan" in removed_paths
    assert not unmanaged.exists()
    # managed skill is kept
    assert (project_root / ".claude" / "skills" / "foo").exists()


def test_prune_removes_unmanaged_rule(home: Path, project_root: Path) -> None:
    rules.add("managed", "Managed rule.", is_default=True)
    init.run(init.InitOptions(project_root=project_root))
    _lock(project_root)

    rules_dir = project_root / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "unmanaged.md").write_text("unmanaged\n")

    result = prune.run(prune.PruneOptions(project_root=project_root))
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert ".claude/rules/unmanaged.md" in removed_paths
    assert not (rules_dir / "unmanaged.md").exists()


def test_prune_removes_unmanaged_mcp_alias(home: Path, project_root: Path) -> None:
    from aim.core import mcp_registry

    init.run(init.InitOptions(project_root=project_root))
    _lock(project_root)
    mcp_path = project_root / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"orphan": {"command": "orphan"}}}))

    result = prune.run(prune.PruneOptions(project_root=project_root))
    removed_aliases = {i.path for i in result.removed if i.kind == "mcp"}
    assert "orphan" in removed_aliases
    data = mcp_registry.read_mcp_json(project_root)
    assert "orphan" not in data.get("mcpServers", {})


def test_prune_aimignore_protects_local_skill(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    _lock(project_root)

    # A local-only skill lives under .claude/skills/local/.
    local = project_root / ".claude" / "skills" / "local" / "handmade"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("handmade\n")
    (project_root / ".aimignore").write_text(".claude/skills/local/*\n")

    result = prune.run(prune.PruneOptions(project_root=project_root))
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert ".claude/skills/local" not in removed_paths
    assert ".claude/skills/local/handmade" not in removed_paths
    assert local.exists()
    assert any(i.path == ".claude/skills/local" and i.action == "skipped" for i in result.kept)


def test_prune_cli_exclude_option(home: Path, project_root: Path) -> None:
    rules.add("managed", "Managed rule.", is_default=True)
    init.run(init.InitOptions(project_root=project_root))
    _lock(project_root)

    rules_dir = project_root / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "unmanaged.md").write_text("unmanaged\n")

    result = prune.run(
        prune.PruneOptions(project_root=project_root, excludes=[".claude/rules/unmanaged.md"])
    )
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert ".claude/rules/unmanaged.md" not in removed_paths
    assert (rules_dir / "unmanaged.md").exists()


def test_prune_aimignore_protects_local_mcp_alias(home: Path, project_root: Path) -> None:
    from aim.core import mcp_registry

    init.run(init.InitOptions(project_root=project_root))
    _lock(project_root)
    mcp_path = project_root / ".mcp.json"
    mcp_path.write_text(
        json.dumps(
            {"mcpServers": {"managed": {"command": "managed"}, "local-db": {"command": "postgres"}}}
        )
    )
    (project_root / ".aimignore").write_text("mcp:local-*\n")

    result = prune.run(prune.PruneOptions(project_root=project_root))
    removed_aliases = {i.path for i in result.removed if i.kind == "mcp"}
    assert "local-db" not in removed_aliases
    assert "managed" in removed_aliases
    kept_aliases = {i.path for i in result.kept if i.kind == "mcp"}
    assert "local-db" in kept_aliases
    data = mcp_registry.read_mcp_json(project_root)
    assert "local-db" in data.get("mcpServers", {})
    assert "managed" not in data.get("mcpServers", {})


def test_prune_malformed_aimignore_is_skipped(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    _lock(project_root)

    local = project_root / ".claude" / "skills" / "local" / "handmade"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("handmade\n")
    # Binary content trips UnicodeDecodeError.
    (project_root / ".aimignore").write_bytes(b"\xff\xfe")

    result = prune.run(prune.PruneOptions(project_root=project_root))
    removed_paths = {i.path for i in result.removed if i.action == "removed"}
    assert ".claude/skills/local" in removed_paths
    assert not local.exists()
