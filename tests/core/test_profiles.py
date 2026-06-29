from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response
from pydantic import ValidationError

from aim.core import (
    agent_install,
    install,
    mcp_install,
    mcp_registry,
    plugin_install,
    profiles,
    repos,
    rule_install,
)
from aim.core import init as init_mod
from tests.fixtures import git_fixtures


def _claude_plugin_repo(tmp_path: Path, name: str = "bare_plugin") -> Path:
    """A repo whose root marketplace exposes one whole-repo claude plugin."""
    import json

    working = git_fixtures.make_source_repo(
        tmp_path / name,
        files={
            ".claude-plugin/marketplace.json": json.dumps(
                {
                    "name": "demo-market",
                    "plugins": [{"name": "superpowers", "source": "./", "version": "1.0.0"}],
                }
            ),
            ".claude-plugin/plugin.json": json.dumps({"name": "superpowers", "version": "1.0.0"}),
            "skills/tdd/SKILL.md": "# tdd\n",
        },
    )
    return git_fixtures.make_bare_remote(working, tmp_path / f"{name}.git")


def test_save_and_load_round_trip(home: Path) -> None:
    p = profiles.Profile(
        name="x",
        symlinks=["CLAUDE.md"],
        rules=[profiles.ProfileRule(qualified_name="repo/a")],
    )
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


def test_from_project_snapshots(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n", "rules/be-concise.md": "Be concise.\n"},
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            symlinks=("CLAUDE.md",),
        )
    )
    install.install(project_root, "anth/foo")
    rule_install.install(project_root, "anth/be-concise")

    snap = profiles.from_project("python-tui", project_root)
    assert snap.name == "python-tui"
    assert "CLAUDE.md" in snap.symlinks
    assert "anth/be-concise" in [r.qualified_name for r in snap.rules]
    assert [s.qualified_name for s in snap.skills] == ["anth/foo"]
    # Artifacts are frozen to their resolved SHAs from the lock.
    assert snap.skills[0].sha is not None


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


def test_apply_reproduces_state(home: Path, project_root: Path, tmp_path: Path) -> None:
    # Build a source project, snapshot it, apply to a new project.
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n", "rules/be-concise.md": "Be concise.\n"},
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md",)))
    install.install(project_root, "anth/foo")
    rule_install.install(project_root, "anth/be-concise")

    profiles.save(profiles.from_project("source", project_root))

    target = tmp_path / "target"
    profiles.apply("source", target)
    target_target = target / ".claude" / "skills" / "foo"
    assert (target_target / "SKILL.md").exists()
    assert (target / "CLAUDE.md").exists()
    from aim.core import manifest

    m = manifest.load(target)
    assert [r.qualified_name for r in m.rules] == ["anth/be-concise"]


def test_from_project_captures_plugins(home: Path, project_root: Path, tmp_path: Path) -> None:
    bare = _claude_plugin_repo(tmp_path)
    repos.add("sp", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    plugin_install.install_plugin(project_root, "sp/superpowers")

    snap = profiles.from_project("with-plugin", project_root)
    assert [(p.qualified_name, p.flavor) for p in snap.plugins] == [("sp/superpowers", "claude")]
    assert snap.plugins[0].sha is not None  # frozen to the locked SHA


def test_plugin_toml_round_trip(home: Path) -> None:
    p = profiles.Profile(
        name="with-plugin",
        plugins=[profiles.ProfilePlugin(qualified_name="repo/sp", sha="a1b2c3d", flavor="claude")],
    )
    loaded = profiles.parse_toml(profiles.render_toml(p))
    assert loaded == p


def test_apply_reproduces_plugin(home: Path, project_root: Path, tmp_path: Path) -> None:
    # Snapshot a project with an installed claude plugin, then apply to a fresh
    # project: the plugin must be vendored AND registered (marketplace + settings).
    bare = _claude_plugin_repo(tmp_path)
    repos.add("sp", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    plugin_install.install_plugin(project_root, "sp/superpowers")
    profiles.save(profiles.from_project("source", project_root))

    target = tmp_path / "target"
    result = profiles.apply("source", target)
    assert result.installed_plugins == ["sp/superpowers"]

    from aim.core import manifest

    m = manifest.load(target)
    assert [(pp.qualified_name, pp.flavor) for pp in m.plugins] == [("sp/superpowers", "claude")]
    # Vendored files present and the claude marketplace + settings wiring written.
    assert (target / ".claude" / "settings.json").exists()
    assert any(
        (target / ".claude" / "plugins").glob("aim-*/superpowers/.claude-plugin/plugin.json")
    )


def test_apply_skips_flavorless_ambiguous_plugin(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # A flavor-less plugin reference (hand-written/older template) whose name
    # resolves to BOTH a claude and an opencode plugin must be skipped on a lenient
    # apply, not crash with an uncaught PluginAmbiguousFlavorError.
    import json

    from aim.core import paths

    targets = paths.user_config_dir() / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / "opencode.toml").write_text(
        'name = "opencode"\n[manifest]\nfile = "package.json"\nname = "name"\n'
        '[register]\nvendor_into = ".opencode/plugins/{name}"\n'
    )
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            ".claude-plugin/marketplace.json": json.dumps(
                {"name": "m", "plugins": [{"name": "dup", "source": "./cdir"}]}
            ),
            "cdir/.claude-plugin/plugin.json": json.dumps({"name": "dup"}),
            "odir/package.json": json.dumps({"name": "dup"}),
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    url = f"file://{bare}"
    repos.add("r", url)
    profiles.save(
        profiles.Profile(
            name="amb",
            repos=[profiles.ProfileRepo(alias="r", url=url)],
            plugins=[profiles.ProfilePlugin(qualified_name="r/dup")],  # no flavor
        )
    )

    result = profiles.apply("amb", tmp_path / "target")
    assert result.skipped_plugins == ["r/dup"]
    assert result.installed_plugins == []


def test_apply_refreshes_stale_registered_repo(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # The real-world failure: a repo is already registered but its cached clone is
    # stale, and the template pins a commit newer than the clone. Apply must refresh
    # the existing repo so the pinned commit is present — not crash on `git archive`.
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo v1\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    url = f"file://{bare}"
    repos.add("r", url)  # clones at v1; the clone is now pinned to that commit

    # Advance the remote past the cached clone — the clone is stale until refreshed.
    new_sha = git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# foo v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)

    profiles.save(
        profiles.Profile(
            name="stale",
            repos=[profiles.ProfileRepo(alias="r", url=url)],
            skills=[profiles.ProfileSkill(qualified_name="r/foo", sha=new_sha)],
        )
    )

    target = tmp_path / "target"
    result = profiles.apply("stale", target)
    assert result.installed_skills == ["r/foo"]
    # Installed at the post-refresh commit, proving the clone was refreshed.
    assert (target / ".claude" / "skills" / "foo" / "SKILL.md").read_text() == "# foo v2\n"


def test_apply_override_risk_acknowledges_artifacts(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # --override-risk must flow through apply into each install, so a template whose
    # artifacts trip the risk gate still applies (the bundle is user-vetted).
    from aim.core import manifest

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    url = f"file://{bare}"
    repos.add("r", url)
    profiles.save(
        profiles.Profile(
            name="t",
            repos=[profiles.ProfileRepo(alias="r", url=url)],
            skills=[profiles.ProfileSkill(qualified_name="r/foo")],
        )
    )

    target = tmp_path / "target"
    profiles.apply("t", target, override_risk=True)
    assert manifest.load(target).skills[0].risk_acknowledged is True


def test_list_and_delete(home: Path) -> None:
    profiles.save(profiles.Profile(name="a"))
    profiles.save(profiles.Profile(name="b"))
    names = [p.name for p in profiles.list_profiles()]
    assert names == ["a", "b"]
    assert profiles.delete("a") is True
    assert profiles.delete("a") is False
    assert [p.name for p in profiles.list_profiles()] == ["b"]


def test_apply_auto_registers_removed_repos(home: Path, project_root: Path, tmp_path: Path) -> None:
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

    # The saved template carries the repo urls, so apply re-registers them.
    target = tmp_path / "target"
    result = profiles.apply("source", target)
    assert result.installed_skills == ["anth/foo"]
    assert result.installed_agents == ["agents/review"]


def test_rename_profile(home: Path) -> None:
    profiles.save(
        profiles.Profile(name="old", rules=[profiles.ProfileRule(qualified_name="repo/a")])
    )
    profile = profiles.load("old")
    renamed = profile.model_copy(update={"name": "new"})
    profiles.save(renamed)
    profiles.delete("old")
    assert [p.name for p in profiles.list_profiles()] == ["new"]
    assert [r.qualified_name for r in profiles.load("new").rules] == ["repo/a"]


def test_toml_round_trip(home: Path) -> None:
    p = profiles.Profile(
        name="my-template",
        symlinks=["CLAUDE.md"],
        rules=[profiles.ProfileRule(qualified_name="repo/be-concise", sha="deadbeef")],
        skills=[profiles.ProfileSkill(qualified_name="repo/skill", sha="a1b2c3d")],
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
instruction_template = "default"

[[rule]]
qualified_name = "repo/a"

[[rule]]
qualified_name = "repo/b"

[[skill]]
qualified_name = "repo/one"

[[skill]]
qualified_name = "repo/two"
sha = "abc123"

[[subagent]]
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
    assert [r.qualified_name for r in p.rules] == ["repo/a", "repo/b"]
    assert [s.qualified_name for s in p.skills] == ["repo/one", "repo/two"]
    assert p.skills[1].sha == "abc123"
    assert [a.qualified_name for a in p.agents] == ["repo/agent"]
    assert [m.alias for m in p.mcp_servers] == ["one", "two"]


def test_toml_round_trip_with_repos_and_description(home: Path) -> None:
    p = profiles.Profile(
        name="my-template",
        description="A template for python services.",
        repos=[profiles.ProfileRepo(alias="acme", url="https://example.com/acme.git")],
        skills=[profiles.ProfileSkill(qualified_name="acme/skill")],
    )
    loaded = profiles.parse_toml(profiles.render_toml(p))
    assert loaded == p
    assert loaded.description == "A template for python services."
    assert loaded.repos == [profiles.ProfileRepo(alias="acme", url="https://example.com/acme.git")]


def test_toml_round_trip_with_archetype(home: Path) -> None:
    p = profiles.Profile(
        name="with-arch",
        archetype=profiles.ProfileArchetype(qualified_name="acme/lean", sha="deadbeef"),
        repos=[profiles.ProfileRepo(alias="acme", url="https://example.com/acme.git")],
    )
    text = profiles.render_toml(p)
    assert "version = 1" in text
    assert "[archetype]" in text
    assert 'qualified_name = "acme/lean"' in text
    loaded = profiles.parse_toml(text)
    assert loaded == p
    assert loaded.archetype.qualified_name == "acme/lean"
    assert loaded.archetype.sha == "deadbeef"


def test_template_default_archetype_is_explicit(home: Path) -> None:
    # The built-in base is stated explicitly, never omitted.
    text = profiles.render_toml(profiles.Profile(name="plain"))
    assert "[archetype]" in text
    assert 'qualified_name = "default"' in text
    assert profiles.parse_toml(text).archetype.is_builtin


def test_template_version_migration(home: Path) -> None:
    # A legacy template with no version parses as the current schema version.
    legacy = profiles.parse_toml('name = "legacy"\n')
    assert legacy.version == profiles.CURRENT_TEMPLATE_VERSION
    # A version newer than supported is rejected.
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(f'version = {profiles.CURRENT_TEMPLATE_VERSION + 1}\nname = "future"\n')
    # A non-int version is rejected.
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml('version = "x"\nname = "bad"\n')
    # Zero / negative versions are rejected (never silently stamped current).
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml('version = 0\nname = "zero"\n')
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml('version = -3\nname = "neg"\n')


def test_render_toml_drops_null_overrides(home: Path) -> None:
    p = profiles.Profile(
        name="x",
        mcp_servers=[
            profiles.ProfileMcpServer(
                registry_name="srv",
                alias="srv",
                overrides={"command": "uvx", "url": None},
            )
        ],
    )
    loaded = profiles.parse_toml(profiles.render_toml(p))
    assert loaded.mcp_servers[0].overrides == {"command": "uvx"}


def test_from_project_records_source_repos(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("anth", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")

    snap = profiles.from_project("src", project_root)
    assert snap.repos == [profiles.ProfileRepo(alias="anth", url=f"file://{bare}")]


def test_resolve_for_apply_auto_registers_missing_repo(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    p = profiles.Profile(
        name="t",
        repos=[profiles.ProfileRepo(alias="acme", url=f"file://{bare}")],
        skills=[profiles.ProfileSkill(qualified_name="acme/foo")],
    )
    resolved = profiles.resolve_for_apply(p, project_root)
    # Registered under its own alias from the embedded url; no rewrite needed.
    assert resolved.skills[0].qualified_name == "acme/foo"
    assert repos.get("acme").url == f"file://{bare}"


def test_resolve_for_apply_rewrites_alias_to_local(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("local", f"file://{bare}")
    p = profiles.Profile(
        name="t",
        repos=[profiles.ProfileRepo(alias="acme", url=f"file://{bare}")],
        skills=[profiles.ProfileSkill(qualified_name="acme/foo")],
        rules=[profiles.ProfileRule(qualified_name="acme/be-concise")],
    )
    resolved = profiles.resolve_for_apply(p, project_root)
    assert resolved.skills[0].qualified_name == "local/foo"
    assert [r.qualified_name for r in resolved.rules] == ["local/be-concise"]


def test_resolve_for_apply_unreachable_repo_raises(home: Path, project_root: Path) -> None:
    from aim.core import git

    p = profiles.Profile(
        name="t",
        repos=[profiles.ProfileRepo(alias="acme", url="file:///does/not/exist.git")],
        skills=[profiles.ProfileSkill(qualified_name="acme/foo")],
    )
    with pytest.raises(git.GitError):
        profiles.resolve_for_apply(p, project_root)


def test_toml_invalid_name(home: Path) -> None:
    text = 'name = "Bad Name"\ninstruction_template = "default"\n'
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)


def test_toml_rejects_yaml(home: Path) -> None:
    text = "name: bad\ninstruction_template: default\n"
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)


def test_toml_rejects_unknown_key(home: Path) -> None:
    text = 'name = "x"\ninstruction_template = "default"\nbad_key = "nope"\n'
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)


def test_toml_rejects_invalid_symlink(home: Path) -> None:
    text = 'name = "x"\ninstruction_template = "default"\nsymlinks = ["../escape.md"]\n'
    with pytest.raises(profiles.ProfileTomlError):
        profiles.parse_toml(text)
