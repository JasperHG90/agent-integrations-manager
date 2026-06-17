from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from aim.core import (
    agent_install,
    content_guard,
    init,
    install,
    layout_profiles,
    manifest,
    mcp_install,
    mcp_registry,
    models,
    repos,
    sync,
)
from tests.fixtures import git_fixtures


def _skill_repo(tmp_path: Path) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n\nFoo skill.\n",
            "skills/foo/extra.md": "supporting content\n",
            "README.md": "fixture\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def _agent_repo(tmp_path: Path) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src-agent",
        files={
            "agents/review/AGENT.md": "---\nname: Review\ndescription: Review a PR\n---\n# Review\n\nBody.\n",
            "README.md": "fixture\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare-agent.git")
    return working, bare


def _mcp_payload(name: str, version: str = "1.0.0") -> dict:
    return {
        "servers": [
            {
                "server": {
                    "name": name,
                    "description": "test",
                    "version": version,
                    "packages": [],
                    "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
                }
            }
        ]
    }


def test_sync_requires_lockfile(project_root: Path) -> None:
    with pytest.raises(sync.SyncError, match=r"no aim.lock.toml"):
        asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))


def test_sync_restores_deleted_skill(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    target = project_root / ".claude" / "skills" / "foo"
    original = (target / "SKILL.md").read_text()
    import shutil

    shutil.rmtree(target)

    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "a/foo" in result.synced_skills
    assert (target / "SKILL.md").read_text() == original


def test_sync_restores_deleted_agent(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _agent_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("anth", f"file://{bare}")
    agent_install.install(project_root, "anth/review")

    target = project_root / ".claude" / "agents" / "review.md"
    original = target.read_text()
    target.unlink()

    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "anth/review" in result.synced_agents
    assert target.read_text() == original


@respx.mock
def test_sync_restores_deleted_mcp_server(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_mcp_payload("srv"))
    )
    mcp_install.install(project_root, "srv", alias="srv")

    data = json.loads((project_root / ".mcp.json").read_text())
    del data["mcpServers"]["srv"]
    (project_root / ".mcp.json").write_text(json.dumps(data))

    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "srv" in result.synced_mcp
    restored = json.loads((project_root / ".mcp.json").read_text())
    assert "srv" in restored["mcpServers"]


def test_sync_detects_skill_local_edits(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    target = project_root / ".claude" / "skills" / "foo" / "SKILL.md"
    target.write_text("tampered")
    with pytest.raises(sync.SyncError, match="edited since install"):
        asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))


@respx.mock
def test_sync_detects_mcp_local_edits(project_root: Path) -> None:
    init.run(init.InitOptions(project_root=project_root))
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_mcp_payload("srv"))
    )
    mcp_install.install(project_root, "srv", alias="srv")

    data = json.loads((project_root / ".mcp.json").read_text())
    data["mcpServers"]["srv"]["url"] = "https://tampered.example.com"
    (project_root / ".mcp.json").write_text(json.dumps(data))

    with pytest.raises(sync.SyncError, match="edited"):
        asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))


def test_sync_overwrites_with_force(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    target = project_root / ".claude" / "skills" / "foo" / "SKILL.md"
    target.write_text("tampered")

    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, force=True)))
    assert "a/foo" in result.synced_skills
    assert "# foo" in target.read_text()


def test_sync_no_changes_on_second_run(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    # install already deployed the exact bytes and recorded the content hash,
    # so the first sync is a no-op.
    first = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "a/foo" not in first.synced_skills
    second = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert second.synced_skills == []


def test_sync_skips_agent_files_when_no_sync_agents(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _agent_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("anth", f"file://{bare}")
    agent_install.install(project_root, "anth/review")

    target = project_root / ".claude" / "agents" / "review.md"
    target.unlink()
    agents_md = project_root / "AGENTS.md"

    result = asyncio.run(
        sync.run(sync.SyncOptions(project_root=project_root, sync_agents=False))
    )
    assert "anth/review" in result.synced_agents
    assert target.exists()
    assert not agents_md.exists()


def test_sync_auto_registers_missing_repo(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    # Install while repo is registered.
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    # Remove the repo from the global registry.
    repos.remove("a")

    target = project_root / ".claude" / "skills" / "foo"
    import shutil

    shutil.rmtree(target)

    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "a/foo" in result.synced_skills
    assert target.exists()


def test_sync_profile_override_writes_correct_agent_file(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _agent_repo(tmp_path)
    # Declare the CLAUDE.md mirror up front so the lock records it; sync reproduces
    # exactly what is in the lock, not undeclared profile defaults.
    init.run(init.InitOptions(project_root=project_root, layout_profile="claude"))
    repos.add("anth", f"file://{bare}")
    agent_install.install(project_root, "anth/review")

    result = asyncio.run(
        sync.run(sync.SyncOptions(project_root=project_root, layout_profile="claude"))
    )
    # install already deployed the agent, so sync only re-renders the mirror.
    assert "anth/review" not in result.synced_agents
    assert (project_root / "CLAUDE.md").exists()


def _malicious_skill_repo(tmp_path: Path) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n\nhidden​\n",
            "README.md": "fixture\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def test_sync_rejects_hidden_unicode_skill(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _malicious_skill_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("a", f"file://{bare}")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=working,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    m = models.Manifest(
        skills=[
            models.InstalledSkill(
                qualified_name="a/foo",
                repo_alias="a",
                repo_url=repos.get("a").url,
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
                current=models.SkillVersion(sha=sha, installed_at=datetime.now(UTC)),
            )
        ]
    )
    manifest.save(project_root, m)

    target = project_root / ".claude" / "skills" / "foo"
    assert not target.exists()
    with pytest.raises(sync.SyncError, match="hidden Unicode"):
        asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert not target.exists()


def test_sync_rejects_hidden_unicode_agents_md(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _agent_repo(tmp_path)
    init.run(init.InitOptions(project_root=project_root))
    repos.add("anth", f"file://{bare}")
    agent_install.install(project_root, "anth/review")

    profile = layout_profiles.resolve_active(project_root)
    agents_path = project_root / profile.agents_md
    agents_path.write_text("# Project\n\nhidden​\n")

    with pytest.raises(content_guard.HiddenUnicodeError):
        asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert "hidden​" in agents_path.read_text()
