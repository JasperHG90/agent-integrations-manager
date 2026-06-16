from __future__ import annotations

from pathlib import Path

import pytest

from agent_init.core import agent_install, init, manifest, repos
from tests.fixtures import git_fixtures


def _make_project_and_repo(tmp_path: Path, project_root: Path) -> tuple[Path, str]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "agents/review/AGENT.md": "---\nname: Review\ndescription: Review a PR\ntools: [git, github]\n---\n# Review\n\nBody.\n",
            "README.md": "x\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    init.run(init.InitOptions(project_root=project_root))
    repos.add("anth", f"file://{bare}")
    return bare, "anth/review"


def test_install_writes_agent_file(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    installed = agent_install.install(project_root, qn)
    assert installed.qualified_name == qn
    target = project_root / ".claude" / "agents" / "review.md"
    assert target.exists()
    assert "# Review" in target.read_text()

    m = manifest.load(project_root)
    assert len(m.agents) == 1
    assert m.agents[0].target_path == ".claude/agents/review.md"
    assert m.agents[0].content_hash is not None


def test_update_refreshes_agent(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    agent_install.install(project_root, qn)

    working = tmp_path / "src"
    git_fixtures.add_commit(
        working, {"agents/review/AGENT.md": "---\nname: Review\n---\n# Updated\n"}, "update agent"
    )
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    result = agent_install.update(project_root, qn)
    assert result.current.sha != result.history[0].sha
    assert "# Updated" in (project_root / ".claude" / "agents" / "review.md").read_text()


def test_update_skips_when_unchanged(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    first = agent_install.install(project_root, qn)
    second = agent_install.update(project_root, qn)
    assert first.current.sha == second.current.sha
    assert second.history == []


def test_update_detects_local_edits(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    agent_install.install(project_root, qn)

    working = tmp_path / "src"
    git_fixtures.add_commit(
        working, {"agents/review/AGENT.md": "---\nname: Review\n---\n# Updated\n"}, "upstream edit"
    )
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    target = project_root / ".claude" / "agents" / "review.md"
    target.write_text("tampered")
    with pytest.raises(agent_install.AgentLocalEditsError):
        agent_install.update(project_root, qn)
    agent_install.update(project_root, qn, force=True)


def test_delete_removes_file_and_manifest_entry(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    agent_install.install(project_root, qn)
    agent_install.delete(project_root, qn)
    assert not (project_root / ".claude" / "agents" / "review.md").exists()
    assert manifest.load(project_root).agents == []


def test_rollback_restores_previous_version(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    agent_install.install(project_root, qn)

    working = tmp_path / "src"
    git_fixtures.add_commit(
        working, {"agents/review/AGENT.md": "---\nname: Review\n---\n# V2\n"}, "v2"
    )
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    agent_install.update(project_root, qn)
    text_after_update = (project_root / ".claude" / "agents" / "review.md").read_text()
    assert "# V2" in text_after_update

    agent_install.rollback(project_root, qn)
    text_after_rollback = (project_root / ".claude" / "agents" / "review.md").read_text()
    assert "# Review" in text_after_rollback


def test_update_many_only_outdated(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    agent_install.install(project_root, qn)
    outcomes = agent_install.update_many(project_root, only_outdated=True)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "noop"


def test_install_uses_tag_for_agent(home: Path, tmp_path: Path, project_root: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "agents/review/AGENT.md": "---\nname: Review\n---\n# Review\n",
            "README.md": "x\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    init.run(init.InitOptions(project_root=project_root))
    repos.add("anth", f"file://{bare}")
    git_fixtures.add_tag(working, "v1.0.0")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    installed = agent_install.install(project_root, "anth/review")
    assert installed.current.tag == "v1.0.0"
