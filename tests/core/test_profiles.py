from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response
from pydantic import ValidationError

from agent_init.core import (
    agent_install,
    install,
    mcp_install,
    mcp_registry,
    profiles,
    repos,
    rules,
)
from agent_init.core import init as init_mod
from tests.fixtures import git_fixtures


def test_save_and_load_round_trip(home: Path) -> None:
    p = profiles.Profile(name="x", template="default", mirrors=["CLAUDE.md"], rules=["a"])
    profiles.save(p)
    loaded = profiles.load("x")
    assert loaded == p


def test_invalid_name_rejected(home: Path) -> None:
    with pytest.raises(ValidationError):
        profiles.Profile(name="Bad Name")


def test_load_missing(home: Path) -> None:
    with pytest.raises(profiles.ProfileNotFoundError):
        profiles.load("ghost")


def _agent_repo(tmp_path: Path) -> Path:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "agents/review/AGENT.md": "---\nname: Review\n---\n# Review\n",
            "README.md": "x\n",
        },
    )
    return git_fixtures.make_bare_remote(working, tmp_path / "bare.git")


def _mcp_payload(name: str) -> dict:
    return {
        "servers": [
            {
                "server": {
                    "name": name,
                    "description": "test",
                    "version": "1.0.0",
                    "packages": [],
                    "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
                }
            }
        ]
    }


@pytest.fixture(autouse=True)
def _clear_mcp_cache():
    mcp_registry._SEARCH_CACHE.clear()
    yield
    mcp_registry._SEARCH_CACHE.clear()


def test_from_project_snapshots(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    rules.add("be-concise", "Be concise.", is_default=True)
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            mirrors=("CLAUDE.md",),
            agent_dialect="claude",
        )
    )
    install.install(project_root, "anth/foo")

    snap = profiles.from_project("python-tui", project_root)
    assert snap.name == "python-tui"
    assert "CLAUDE.md" in snap.mirrors
    assert "be-concise" in snap.rules
    assert snap.agent_dialect == "claude"
    assert [s.qualified_name for s in snap.skills] == ["anth/foo"]


@respx.mock
def test_from_project_captures_agents_and_mcp(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    skill_working = git_fixtures.make_source_repo(
        tmp_path / "src_skill", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    skill_bare = git_fixtures.make_bare_remote(skill_working, tmp_path / "bare_skill.git")
    agent_bare = _agent_repo(tmp_path)
    repos.add("anth", f"file://{skill_bare}")
    repos.add("agents", f"file://{agent_bare}")
    rules.add("be-concise", "Be concise.", is_default=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")
    agent_install.install(project_root, "agents/review")
    respx.get(f"{mcp_registry._REGISTRY_BASE}").mock(
        return_value=Response(200, json=_mcp_payload("my-server"))
    )
    mcp_install.install(project_root, "my-server", alias="my")

    snap = profiles.from_project("full", project_root)
    assert [s.qualified_name for s in snap.skills] == ["anth/foo"]
    assert [a.qualified_name for a in snap.agents] == ["agents/review"]
    assert [(m.registry_name, m.alias) for m in snap.mcp_servers] == [("my-server", "my")]
    assert snap.mcp_servers[0].transport == "http"


def test_apply_reproduces_state(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Build a source project, snapshot it, apply to a new project.
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    rules.add("be-concise", "Be concise.", is_default=True)
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root, mirrors=("CLAUDE.md",)
        )
    )
    install.install(project_root, "anth/foo")

    profiles.save(profiles.from_project("source", project_root))

    target = tmp_path / "target"
    profiles.apply("source", target)
    target_target = target / ".claude" / "skills" / "foo"
    assert (target_target / "SKILL.md").exists()
    assert (target / "CLAUDE.md").exists()
    from agent_init.core import manifest

    m = manifest.load(target)
    assert "be-concise" in m.rules


def test_list_and_delete(home: Path) -> None:
    profiles.save(profiles.Profile(name="a"))
    profiles.save(profiles.Profile(name="b"))
    names = [p.name for p in profiles.list_profiles()]
    assert names == ["a", "b"]
    assert profiles.delete("a") is True
    assert profiles.delete("a") is False
    assert [p.name for p in profiles.list_profiles()] == ["b"]


def test_apply_reports_skipped_items(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src_skill", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare_skill.git")
    agent_working = git_fixtures.make_source_repo(
        tmp_path / "src_agent",
        files={
            "agents/review/AGENT.md": "---\nname: Review\n---\n# Review\n",
            "README.md": "x\n",
        },
    )
    agent_bare = git_fixtures.make_bare_remote(agent_working, tmp_path / "bare_agent.git")
    repos.add("anth", f"file://{bare}")
    repos.add("agents", f"file://{agent_bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")
    agent_install.install(project_root, "agents/review")

    profiles.save(profiles.from_project("source", project_root))

    repos.remove("anth")
    repos.remove("agents")

    target = tmp_path / "target"
    result = profiles.apply("source", target)
    assert result.installed_skills == []
    assert result.skipped_skills == ["anth/foo"]
    assert result.installed_agents == []
    assert result.skipped_agents == ["agents/review"]


def test_rename_profile(home: Path) -> None:
    profiles.save(profiles.Profile(name="old", rules=["a"]))
    profile = profiles.load("old")
    renamed = profile.model_copy(update={"name": "new"})
    profiles.save(renamed)
    profiles.delete("old")
    assert [p.name for p in profiles.list_profiles()] == ["new"]
    assert profiles.load("new").rules == ["a"]


def test_toml_round_trip(home: Path) -> None:
    p = profiles.Profile(
        name="my-template",
        template="default",
        mirrors=["CLAUDE.md"],
        symlinks=[],
        rules=["be-concise"],
        skills=[profiles.ProfileSkill(qualified_name="repo/skill", pin="v1.0.0")],
        agents=[profiles.ProfileAgent(qualified_name="repo/agent")],
        mcp_servers=[
            profiles.ProfileMcpServer(
                registry_name="srv",
                alias="srv",
                transport="stdio",
                overrides={"command": "uvx"},
            )
        ],
    )
    text = profiles.render_toml(p)
    loaded = profiles.parse_toml(text)
    assert loaded == p


def test_toml_multiple_items(home: Path) -> None:
    text = """
name = "multi"
template = "default"
rules = ["a", "b"]

[[skill]]
qualified_name = "repo/one"

[[skill]]
qualified_name = "repo/two"
pin = "v2"

[[agent]]
qualified_name = "repo/agent"

[[mcp_server]]
registry_name = "srv-one"
alias = "one"

[[mcp_server]]
registry_name = "srv-two"
alias = "two"
transport = "http"
"""
    p = profiles.parse_toml(text)
    assert p.name == "multi"
    assert [s.qualified_name for s in p.skills] == ["repo/one", "repo/two"]
    assert p.skills[1].pin == "v2"
    assert [a.qualified_name for a in p.agents] == ["repo/agent"]
    assert [m.alias for m in p.mcp_servers] == ["one", "two"]


def test_toml_invalid_name(home: Path) -> None:
    text = 'name = "Bad Name"\ntemplate = "default"\n'
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)


def test_toml_rejects_yaml(home: Path) -> None:
    text = "name: bad\ntemplate: default\n"
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)


def test_toml_rejects_unknown_key(home: Path) -> None:
    text = 'name = "x"\ntemplate = "default"\nbad_key = "nope"\n'
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)


def test_toml_rejects_invalid_mirror(home: Path) -> None:
    text = 'name = "x"\ntemplate = "default"\nmirrors = ["../escape.md"]\n'
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)
