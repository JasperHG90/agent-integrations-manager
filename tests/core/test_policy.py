"""Tests for the policy/governance spine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aim import cli
from aim.core import (
    agent_install,
    content_guard,
    declarations,
    layout_profiles,
    lock,
    manifest,
    manifest_migrate,
    policy,
    repos,
    rule_install,
)
from aim.core import init as init_mod
from aim.core.models import (
    DeclaredAgent,
    DeclaredMcpServer,
    DeclaredRule,
    DeclaredSkill,
    Manifest,
    ProjectDeclarations,
)
from tests.fixtures import git_fixtures

# ---------------------------------------------------------------------------
# resolution & precedence
# ---------------------------------------------------------------------------


def test_resolve_effective_builtin_when_no_local(home: Path) -> None:
    resolved = policy.resolve_effective()
    assert resolved.source == "builtin"
    assert resolved.repo is None
    assert resolved.hash is None


def test_resolve_effective_local_when_saved(home: Path) -> None:
    pol = policy.Policy(name="acme", blocked_skills=["r/bad"])
    policy.save_local_policy(pol)
    resolved = policy.resolve_effective()
    assert resolved.source == "local"
    assert resolved.policy.name == "acme"
    assert resolved.hash == policy.compute_hash(resolved.policy)


# ---------------------------------------------------------------------------
# hashing & serialization
# ---------------------------------------------------------------------------


def test_compute_hash_stable_and_sensitive() -> None:
    a = policy.Policy(name="x", blocked_repos=["u"])
    b = policy.Policy(name="x", blocked_repos=["u"])
    c = policy.Policy(name="x", blocked_repos=["u", "v"])
    assert policy.compute_hash(a) == policy.compute_hash(b)
    assert policy.compute_hash(a) != policy.compute_hash(c)


def test_toml_roundtrip_preserves_fields() -> None:
    pol = policy.Policy(
        name="acme",
        blocked_repos=["git@github.com:evil/x.git"],
        blocked_skills=["r/bad"],
        allowed_profiles=["claude"],
    )
    pol.risk.enabled = True
    pol.risk.preset_overrides = {"obfuscation": False, "destructive_ops": "medium"}
    back = policy.from_toml(policy.to_toml(pol))
    assert back.blocked_repos == pol.blocked_repos
    assert back.blocked_skills == pol.blocked_skills
    assert back.allowed_profiles == pol.allowed_profiles
    assert back.risk.enabled is True
    assert back.risk.preset_overrides == {"obfuscation": False, "destructive_ops": "medium"}


def test_rules_toml_roundtrip() -> None:
    rules = [
        policy.RiskRule(id="calls_internal_api", severity="medium", prompt="Flag X."),
        policy.RiskRule(id="other", severity="high", prompt="Flag Y."),
    ]
    back = policy.parse_rules_toml(policy.render_rules_toml(rules))
    assert [r.id for r in back] == ["calls_internal_api", "other"]
    assert back[0].severity == "medium"
    assert back[0].prompt == "Flag X."


# ---------------------------------------------------------------------------
# DB storage
# ---------------------------------------------------------------------------


def test_local_policy_db_roundtrip_with_custom_rules(home: Path) -> None:
    pol = policy.Policy(name="local", blocked_agents=["r/a"])
    pol.custom_rules = [policy.RiskRule(id="c1", severity="low", prompt="p")]
    policy.save_local_policy(pol)
    loaded = policy.load_local_policy()
    assert loaded is not None
    assert loaded.blocked_agents == ["r/a"]
    assert [r.id for r in loaded.custom_rules] == ["c1"]
    policy.clear_local_policy()
    assert policy.load_local_policy() is None


def test_binding_db_roundtrip(home: Path) -> None:
    assert policy.get_binding() is None
    policy.set_binding("https://example.com/policy.git", "main")
    assert policy.get_binding() == {"repo": "https://example.com/policy.git", "ref": "main"}
    policy.clear_binding()
    assert policy.get_binding() is None


# ---------------------------------------------------------------------------
# active rules
# ---------------------------------------------------------------------------


def test_active_rules_overrides_disable_and_custom() -> None:
    pol = policy.Policy()
    pol.risk.preset_overrides = {"obfuscation": False, "secret_exfiltration": "medium"}
    pol.custom_rules = [policy.RiskRule(id="c1", severity="high", prompt="p")]
    rules = {r.id: r for r in policy.active_rules(pol)}
    assert "obfuscation" not in rules  # disabled
    assert rules["secret_exfiltration"].severity == "medium"  # overridden
    assert "c1" in rules  # custom included

    pol.risk.load_custom = False
    assert "c1" not in {r.id for r in policy.active_rules(pol)}


# ---------------------------------------------------------------------------
# enforcement predicates
# ---------------------------------------------------------------------------


def test_normalize_repo_url() -> None:
    assert policy.normalize_repo_url("git@github.com:Evil/X.git") == "https://github.com/evil/x"
    assert policy.normalize_repo_url("https://github.com/evil/x/") == "https://github.com/evil/x"


def test_assert_repo_allowed_by_url_and_alias() -> None:
    pol = policy.Policy(name="p", blocked_repos=["https://github.com/evil/x", "badalias"])
    with pytest.raises(policy.PolicyViolationError, match="blocked"):
        policy.assert_repo_allowed(pol, "anything", "git@github.com:evil/x.git")
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_repo_allowed(pol, "badalias", "https://ok.example/repo")
    policy.assert_repo_allowed(pol, "good", "https://github.com/good/repo")  # no raise


def test_assert_artifact_allowed() -> None:
    pol = policy.Policy(name="p", blocked_skills=["r/bad"], blocked_agents=["r/a"])
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_artifact_allowed(pol, "skill", "r/bad")
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_artifact_allowed(pol, "agent", "r/a")
    policy.assert_artifact_allowed(pol, "skill", "r/ok")  # no raise
    policy.assert_artifact_allowed(pol, "rule", "r/bad")  # different kind: allowed


def test_assert_profile_allowed() -> None:
    pol = policy.Policy(name="p", allowed_profiles=["claude"])
    policy.assert_profile_allowed(pol, "claude")
    policy.assert_profile_allowed(pol, None)  # None is always fine
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_profile_allowed(pol, "gemini")
    policy.assert_profile_allowed(policy.Policy(), "anything")  # empty allow-list = all


# ---------------------------------------------------------------------------
# lock-time enforcement & recording
# ---------------------------------------------------------------------------


def _setup_locked_skill_project(project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = ProjectDeclarations(
        repos={"a": "file://placeholder"},
        skills=[
            DeclaredSkill(
                qualified_name="a/foo",
                repo_alias="a",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            )
        ],
    )
    declarations.save(project_root, decl)


def test_lock_refuses_blocked_skill(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    policy.save_local_policy(policy.Policy(name="local", blocked_skills=["a/foo"]))
    with pytest.raises(policy.PolicyViolationError, match="a/foo"):
        asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))


def test_lock_records_policy_hash(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    policy.save_local_policy(policy.Policy(name="local"))
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    saved = policy.load_local_policy()
    assert saved is not None
    assert m.policy_repo is None
    assert m.policy_hash == policy.compute_hash(saved)


def test_lock_no_policy_omits_fields(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.policy_hash is None
    assert "policy_hash" not in (project_root / "aim.lock.toml").read_text()


# ---------------------------------------------------------------------------
# `aim policy validate` (the CI gate)
# ---------------------------------------------------------------------------


_runner = CliRunner()


def _write_declarations(project_root: Path, **kwargs: Any) -> None:
    declarations.save(project_root, ProjectDeclarations(**kwargs))


def _validate(project_root: Path, *extra: str) -> int:
    """Invoke `aim policy validate` via the real CLI; return the exit code."""
    result = _runner.invoke(cli.app, ["policy", "validate", str(project_root), *extra])
    return result.exit_code


def test_policy_validate_blocks_disallowed(home: Path, project_root: Path) -> None:
    _write_declarations(
        project_root,
        skills=[
            DeclaredSkill(
                qualified_name="a/bad",
                repo_alias="a",
                source_path="skills/bad",
                target_dir=".claude/skills/bad",
            )
        ],
    )
    policy.save_local_policy(policy.Policy(name="local", blocked_skills=["a/bad"]))
    assert _validate(project_root) == 1


def test_policy_validate_ok_when_clean(home: Path, project_root: Path) -> None:
    _write_declarations(
        project_root,
        agents=[
            DeclaredAgent(
                qualified_name="a/ok",
                repo_alias="a",
                source_path="agents/ok",
                target_path=".claude/agents/ok.md",
            )
        ],
        rules=[DeclaredRule(qualified_name="a/r", repo_alias="a", source_path="rules/r.md")],
    )
    policy.save_local_policy(policy.Policy(name="local"))
    assert _validate(project_root) == 0


def test_policy_validate_no_aim_toml_is_clean(home: Path, project_root: Path) -> None:
    assert _validate(project_root) == 0  # no aim.toml -> clean pass


def test_policy_validate_blocks_mcp(home: Path, project_root: Path) -> None:
    _write_declarations(
        project_root,
        mcp_servers=[DeclaredMcpServer(alias="gh", registry_name="github")],
    )
    policy.save_local_policy(policy.Policy(name="local", blocked_mcp=["github"]))
    assert _validate(project_root) == 1


# ---------------------------------------------------------------------------
# deploy-gate wiring (the security boundary) — fast, no git
# ---------------------------------------------------------------------------


def test_gate_agent_blocks_artifact(home: Path) -> None:
    policy.save_local_policy(policy.Policy(name="local", blocked_agents=["r/a"]))
    with pytest.raises(policy.PolicyViolationError):
        agent_install._gate_agent("r/a", "content")
    agent_install._gate_agent("r/ok", "content")  # not blocked


def test_gate_rule_blocks_artifact(home: Path) -> None:
    policy.save_local_policy(policy.Policy(name="local", blocked_rules=["r/bad"]))
    with pytest.raises(policy.PolicyViolationError):
        rule_install._gate_rule("r/bad", "content")
    rule_install._gate_rule("r/ok", "content")  # not blocked


def test_gates_still_enforce_hidden_unicode_after_refactor(home: Path) -> None:
    # Phase 0 moved the hidden-unicode scan into the gates; confirm it still fires.
    with pytest.raises(content_guard.HiddenUnicodeError):
        agent_install._gate_agent("r/ok", "hello​world")
    with pytest.raises(content_guard.HiddenUnicodeError):
        rule_install._gate_rule("r/ok", "hello​world")


def test_repos_add_gate_blocks_before_clone(home: Path) -> None:
    policy.save_local_policy(policy.Policy(name="local", blocked_repos=["https://evil/x"]))
    # The gate runs before any clone, so a bogus URL never gets fetched.
    with pytest.raises(policy.PolicyViolationError):
        repos.add("evil", "https://evil/x")


# ---------------------------------------------------------------------------
# profile-allow-list bypass regression (P0) + set_active / init gates
# ---------------------------------------------------------------------------


def test_lock_enforce_profile_bypass_regression() -> None:
    # decl.layout_profile is None, but the effective profile ("claude") must still
    # be checked against the allow-list — otherwise the default profile bypasses it.
    decl = ProjectDeclarations(layout_profile=None)
    resolved = policy.ResolvedPolicy(
        policy.Policy(name="p", allowed_profiles=["gemini"]), "local", None, "h"
    )
    with pytest.raises(policy.PolicyViolationError, match="claude"):
        lock._enforce_policy(decl, resolved, effective_profile="claude")


def test_lock_enforce_blocks_mcp() -> None:
    decl = ProjectDeclarations(mcp_servers=[DeclaredMcpServer(alias="gh", registry_name="github")])
    resolved = policy.ResolvedPolicy(
        policy.Policy(name="p", blocked_mcp=["github"]), "local", None, "h"
    )
    with pytest.raises(policy.PolicyViolationError):
        lock._enforce_policy(decl, resolved, effective_profile="claude")


def test_set_active_gate_blocks_disallowed_profile(home: Path, project_root: Path) -> None:
    policy.save_local_policy(policy.Policy(name="local", allowed_profiles=["claude"]))
    with pytest.raises(policy.PolicyViolationError):
        layout_profiles.set_active(project_root, "gemini")


def test_init_gate_blocks_default_profile_not_in_allowlist(home: Path, project_root: Path) -> None:
    # Default profile is "claude"; an allow-list of only "gemini" must reject init.
    policy.save_local_policy(policy.Policy(name="local", allowed_profiles=["gemini"]))
    with pytest.raises(policy.PolicyViolationError):
        init_mod.run(init_mod.InitOptions(project_root=project_root))


# ---------------------------------------------------------------------------
# migration & lockfile round-trip
# ---------------------------------------------------------------------------


def test_migration_v8_to_v9_additive() -> None:
    raw = {"manifest_version": 8, "skills": [], "rules": []}
    out = manifest_migrate.migrate(dict(raw))
    assert out["manifest_version"] == 9
    assert out["policy_repo"] is None
    assert out["policy_hash"] is None


def test_lockfile_preserves_set_policy_hash(home: Path, project_root: Path) -> None:
    m = Manifest(policy_repo="https://example/p", policy_hash="deadbeef")
    manifest.save(project_root, m)
    loaded = manifest.load(project_root)
    assert loaded.policy_repo == "https://example/p"
    assert loaded.policy_hash == "deadbeef"


# ---------------------------------------------------------------------------
# Phase 2 — org policy repo + binding
# ---------------------------------------------------------------------------


def _make_policy_repo(
    tmp_path: Path, policy_toml: str, rules_toml: str | None = None, name: str = "polrepo"
) -> str:
    files = {"policy.toml": policy_toml}
    if rules_toml is not None:
        files["aim.rules.toml"] = rules_toml
    working = git_fixtures.make_source_repo(tmp_path / name, files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / f"{name}.git")
    return f"file://{bare}"


def test_bind_fetches_pins_and_loads_custom_rules(home: Path, tmp_path: Path) -> None:
    url = _make_policy_repo(
        tmp_path,
        'version = 1\nname = "acme"\n[artifacts]\nblocked_skills = ["a/foo"]\n',
        rules_toml='[[rule]]\nid = "c1"\nseverity = "high"\nprompt = "p"\n',
    )
    resolved = policy.bind(url)
    assert resolved.source == "org"
    assert resolved.policy.name == "acme"
    assert resolved.policy.blocked_skills == ["a/foo"]
    assert [r.id for r in resolved.policy.custom_rules] == ["c1"]
    assert policy.get_binding() is not None
    assert policy.load_org_snapshot() is not None


def test_org_policy_replaces_local(home: Path, tmp_path: Path) -> None:
    policy.save_local_policy(policy.Policy(name="local", blocked_skills=["x/y"]))
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    resolved = policy.resolve_effective()
    assert resolved.source == "org"
    assert resolved.policy.name == "acme"


def test_resolve_offline_uses_snapshot_after_remote_gone(home: Path, tmp_path: Path) -> None:
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    # Delete the bare remote AND the cached clone: resolution must still work from
    # the DB snapshot (proves `lock`/resolve never need the network).
    import shutil

    shutil.rmtree(tmp_path / "polrepo.git")
    shutil.rmtree(policy._policy_clone_dir(url), ignore_errors=True)
    resolved = policy.resolve_effective()
    assert resolved.source == "org"
    assert resolved.policy.name == "acme"


def test_unbind_falls_back_to_local(home: Path, tmp_path: Path) -> None:
    policy.save_local_policy(policy.Policy(name="local"))
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    assert policy.resolve_effective().source == "org"
    policy.unbind()
    assert policy.get_binding() is None
    assert policy.resolve_effective().source == "local"


def test_lock_records_org_policy(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    bound = policy.bind(url)
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.policy_repo == url
    assert m.policy_hash == bound.hash


def test_org_policy_blocks_skill_at_lock(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    url = _make_policy_repo(
        tmp_path, 'version = 1\nname = "acme"\n[artifacts]\nblocked_skills = ["a/foo"]\n'
    )
    policy.bind(url)
    with pytest.raises(policy.PolicyViolationError, match="a/foo"):
        asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))


def test_validate_against_remote_policy_cli(home: Path, project_root: Path, tmp_path: Path) -> None:
    _write_declarations(
        project_root,
        skills=[
            DeclaredSkill(
                qualified_name="a/foo",
                repo_alias="a",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            )
        ],
    )
    url = _make_policy_repo(
        tmp_path, 'version = 1\nname = "acme"\n[artifacts]\nblocked_skills = ["a/foo"]\n'
    )
    # No local/org binding; --policy fetches the remote fresh (the out-of-band gate).
    assert _validate(project_root, "--policy", url) == 1
