"""Tests for the policy/governance spine (aim.toml [policy] model)."""

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
# helpers: write the [policy] table into a project's aim.toml
# ---------------------------------------------------------------------------


def _set_local(project_root: Path, pol: policy.Policy) -> None:
    section = policy.to_mapping(pol)
    section["scope"] = "local"
    policy.set_project_policy(project_root, section)


def _set_org(project_root: Path, repo: str, ref: str = "HEAD") -> None:
    policy.set_project_policy(project_root, {"scope": "org", "repo": repo, "ref": ref})


def _write_declarations(project_root: Path, **kwargs: Any) -> None:
    declarations.save(project_root, ProjectDeclarations(**kwargs))


# ---------------------------------------------------------------------------
# resolution from aim.toml
# ---------------------------------------------------------------------------


def test_resolve_builtin_when_no_policy(home: Path, project_root: Path) -> None:
    resolved = policy.resolve_effective(project_root)
    assert resolved.source == "builtin"
    assert resolved.repo is None and resolved.hash is None


def test_resolve_local_from_aim_toml(home: Path, project_root: Path) -> None:
    _set_local(project_root, policy.Policy(name="acme", blocked_skills=["r/bad"]))
    resolved = policy.resolve_effective(project_root)
    assert resolved.source == "local"
    assert resolved.policy.blocked_skills == ["r/bad"]
    assert resolved.hash == policy.compute_hash(resolved.policy)
    # cheap gate path agrees
    assert policy.effective_policy(project_root).blocked_skills == ["r/bad"]


def test_resolve_none_project_root_is_builtin(home: Path) -> None:
    assert policy.resolve_effective(None).source == "builtin"
    assert policy.effective_policy(None).blocked_skills == []


def test_local_policy_roundtrips_custom_rules(home: Path, project_root: Path) -> None:
    pol = policy.Policy(name="local", blocked_agents=["r/a"])
    pol.custom_rules = [policy.RiskRule(id="c1", severity="low", prompt="p")]
    _set_local(project_root, pol)
    back = policy.resolve_effective(project_root).policy
    assert back.blocked_agents == ["r/a"]
    assert [r.id for r in back.custom_rules] == ["c1"]


# ---------------------------------------------------------------------------
# hashing & serialization (pure)
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
    pol.risk.allow_override = False
    pol.risk.classifier = True
    pol.risk.llm_judge = True
    pol.risk.preset_overrides = {"obfuscation": False, "destructive_ops": "medium"}
    back = policy.from_toml(policy.to_toml(pol))
    assert back.blocked_repos == pol.blocked_repos
    assert back.blocked_skills == pol.blocked_skills
    assert back.allowed_profiles == pol.allowed_profiles
    assert back.risk.classifier is True
    assert back.risk.allow_override is False
    assert back.risk.classifier is True and back.risk.llm_judge is True
    assert back.risk.preset_overrides == {"obfuscation": False, "destructive_ops": "medium"}


def test_from_mapping_reads_inline_custom_rules() -> None:
    pol = policy.from_mapping(
        {
            "artifacts": {"blocked_skills": ["a/b"]},
            "risk": {"classifier": True},
            "rule": [{"id": "x", "severity": "high", "prompt": "p"}],
        }
    )
    assert pol.blocked_skills == ["a/b"]
    assert pol.risk.classifier is True
    assert [r.id for r in pol.custom_rules] == ["x"]


def test_rules_toml_roundtrip() -> None:
    rules = [
        policy.RiskRule(id="calls_internal_api", severity="medium", prompt="Flag X."),
        policy.RiskRule(id="other", severity="high", prompt="Flag Y."),
    ]
    back = policy.parse_rules_toml(policy.render_rules_toml(rules))
    assert [r.id for r in back] == ["calls_internal_api", "other"]


def test_active_rules_overrides_disable_and_custom() -> None:
    pol = policy.Policy()
    pol.risk.preset_overrides = {"obfuscation": False, "secret_exfiltration": "medium"}
    pol.custom_rules = [policy.RiskRule(id="c1", severity="high", prompt="p")]
    rules = {r.id: r for r in policy.active_rules(pol)}
    assert "obfuscation" not in rules
    assert rules["secret_exfiltration"].severity == "medium"
    assert "c1" in rules
    pol.risk.load_custom = False
    assert "c1" not in {r.id for r in policy.active_rules(pol)}


def test_risk_per_kind_resolve_and_roundtrip() -> None:
    pol = policy.from_mapping(
        {
            "risk": {
                "classifier": True,
                "llm_judge": False,
                "skill": {"classifier": False},
                "plugin": {"llm_judge": True},
            }
        }
    )
    assert pol.risk.resolve("skill") == (False, False)  # classifier overridden off
    assert pol.risk.resolve("plugin") == (True, True)  # llm_judge overridden on
    assert pol.risk.resolve("agent") == (True, False)  # inherits the global flags
    assert pol.risk.active_for("skill") is False
    assert pol.risk.active_for("plugin") is True
    # round-trip through TOML preserves the per-kind overrides
    back = policy.from_toml(policy.to_toml(pol))
    assert back.risk.resolve("skill") == (False, False)
    assert back.risk.resolve("plugin") == (True, True)


# ---------------------------------------------------------------------------
# enforcement predicates (pure)
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
    policy.assert_repo_allowed(pol, "good", "https://github.com/good/repo")


def test_assert_artifact_allowed() -> None:
    pol = policy.Policy(name="p", blocked_skills=["r/bad"], blocked_agents=["r/a"])
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_artifact_allowed(pol, "skill", "r/bad")
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_artifact_allowed(pol, "agent", "r/a")
    policy.assert_artifact_allowed(pol, "skill", "r/ok")
    policy.assert_artifact_allowed(pol, "rule", "r/bad")  # different kind: allowed


def test_assert_profile_allowed() -> None:
    pol = policy.Policy(name="p", allowed_profiles=["claude"])
    policy.assert_profile_allowed(pol, "claude")
    policy.assert_profile_allowed(pol, None)
    with pytest.raises(policy.PolicyViolationError):
        policy.assert_profile_allowed(pol, "gemini")
    policy.assert_profile_allowed(policy.Policy(), "anything")  # empty = all allowed


# ---------------------------------------------------------------------------
# deploy-gate wiring (the security boundary), reading aim.toml
# ---------------------------------------------------------------------------


def test_gate_agent_blocks_artifact(home: Path, project_root: Path) -> None:
    _set_local(project_root, policy.Policy(name="local", blocked_agents=["r/a"]))
    with pytest.raises(policy.PolicyViolationError):
        agent_install._gate_agent(project_root, "r/a", "content")
    agent_install._gate_agent(project_root, "r/ok", "content")  # not blocked


def test_gate_rule_blocks_artifact(home: Path, project_root: Path) -> None:
    _set_local(project_root, policy.Policy(name="local", blocked_rules=["r/bad"]))
    with pytest.raises(policy.PolicyViolationError):
        rule_install._gate_rule(project_root, "r/bad", "content")
    rule_install._gate_rule(project_root, "r/ok", "content")


def test_gates_still_enforce_hidden_unicode(home: Path, project_root: Path) -> None:
    with pytest.raises(content_guard.HiddenUnicodeError):
        agent_install._gate_agent(project_root, "r/ok", "hello​world")
    with pytest.raises(content_guard.HiddenUnicodeError):
        rule_install._gate_rule(project_root, "r/ok", "hello​world")


# ---------------------------------------------------------------------------
# profile gates (init / set_active) read aim.toml
# ---------------------------------------------------------------------------


def test_set_active_gate_blocks_disallowed_profile(home: Path, project_root: Path) -> None:
    _set_local(project_root, policy.Policy(name="local", allowed_profiles=["claude"]))
    with pytest.raises(policy.PolicyViolationError):
        layout_profiles.set_active(project_root, "gemini")


def test_init_gate_blocks_default_profile_not_in_allowlist(home: Path, project_root: Path) -> None:
    # Pre-write a policy that only allows gemini; re-init resolves the default
    # profile (claude) and must reject it.
    _set_local(project_root, policy.Policy(name="local", allowed_profiles=["gemini"]))
    with pytest.raises(policy.PolicyViolationError):
        init_mod.run(init_mod.InitOptions(project_root=project_root))


def test_lock_enforce_profile_bypass_regression() -> None:
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


# ---------------------------------------------------------------------------
# lock-time enforcement & recording (local policy from aim.toml)
# ---------------------------------------------------------------------------


def _setup_locked_skill_project(project_root: Path, tmp_path: Path) -> str:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repo_url = f"file://{bare}"
    repos.add("a", repo_url)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = ProjectDeclarations(
        repos={"a": repo_url},
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
    return repo_url


def test_lock_refuses_blocked_skill(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    _set_local(project_root, policy.Policy(name="local", blocked_skills=["a/foo"]))
    with pytest.raises(policy.PolicyViolationError, match="a/foo"):
        asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))


def test_lock_refuses_blocked_repo(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    _set_local(project_root, policy.Policy(name="local", blocked_repos=["a"]))
    with pytest.raises(policy.PolicyViolationError):
        asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))


def test_install_refuses_blocked_repo_by_alias(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import install

    _setup_locked_skill_project(project_root, tmp_path)
    # Block the REPO (not the artifact) — install must refuse at deploy, before lock.
    _set_local(project_root, policy.Policy(name="local", blocked_repos=["a"]))
    with pytest.raises(policy.PolicyViolationError):
        install.install(project_root, "a/foo")


def test_install_refuses_blocked_repo_by_url(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import install

    url = _setup_locked_skill_project(project_root, tmp_path)
    _set_local(project_root, policy.Policy(name="local", blocked_repos=[url]))
    with pytest.raises(policy.PolicyViolationError):
        install.install(project_root, "a/foo")


def test_lock_records_local_policy_hash(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    _set_local(project_root, policy.Policy(name="local"))
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.policy_repo is None and m.policy_ref is None
    assert m.policy_hash == policy.resolve_effective(project_root).hash


def test_lock_no_policy_omits_fields(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    # remove the [policy] table that init seeded (builtin permissive)
    policy.set_project_policy(project_root, {})
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.policy_hash is None
    assert "policy_hash" not in (project_root / "aim.lock.toml").read_text()


# ---------------------------------------------------------------------------
# org policy repo: fetch + per-repo snapshot cache + offline resolution
# ---------------------------------------------------------------------------


def _make_policy_repo(tmp_path: Path, policy_toml: str, name: str = "polrepo") -> str:
    working = git_fixtures.make_source_repo(tmp_path / name, files={"policy.toml": policy_toml})
    bare = git_fixtures.make_bare_remote(working, tmp_path / f"{name}.git")
    return f"file://{bare}"


def test_init_seeds_default_local_policy(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    assert policy.project_policy_section(project_root) == {"scope": "local"}


def test_org_snapshots_keyed_per_repo(home: Path, tmp_path: Path) -> None:
    url1 = _make_policy_repo(tmp_path, 'version = 1\nname = "one"\n', name="p1")
    url2 = _make_policy_repo(tmp_path, 'version = 1\nname = "two"\n', name="p2")
    policy.bind(url1)
    policy.bind(url2)
    s1 = policy.load_org_snapshot(url1)
    s2 = policy.load_org_snapshot(url2)
    assert s1 is not None and s1.policy.name == "one"
    assert s2 is not None and s2.policy.name == "two"


def _backdate_org_snapshot(url: str, *, days: int) -> None:
    import json as _json
    from datetime import UTC, datetime, timedelta

    from aim.core import db
    from aim.core.models import GlobalSetting

    with db.session() as s:
        row = s.get(GlobalSetting, policy._org_snapshot_key(url))
        data = _json.loads(row.value)
        data["fetched_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        row.value = _json.dumps(data)
        s.commit()


def test_org_ttl_refreshes_when_stale(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    _backdate_org_snapshot(url, days=2)
    policy.reset_refresh_state()
    before = policy._snapshot_fetched_at(url)
    policy.resolve_effective(project_root)  # stale -> opportunistic re-fetch
    after = policy._snapshot_fetched_at(url)
    assert before is not None and after is not None and after > before


def test_org_ttl_no_refresh_when_fresh(home: Path, project_root: Path, tmp_path: Path) -> None:
    import shutil

    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    policy.reset_refresh_state()
    before = policy._snapshot_fetched_at(url)
    # Remove the remote: a fresh snapshot must NOT attempt a network fetch.
    shutil.rmtree(tmp_path / "polrepo.git")
    shutil.rmtree(policy._policy_clone_dir(url), ignore_errors=True)
    policy.resolve_effective(project_root)
    assert policy._snapshot_fetched_at(url) == before


def test_org_ttl_offline_keeps_stale_cache(home: Path, project_root: Path, tmp_path: Path) -> None:
    import shutil

    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    _backdate_org_snapshot(url, days=2)
    shutil.rmtree(tmp_path / "polrepo.git")
    shutil.rmtree(policy._policy_clone_dir(url), ignore_errors=True)
    policy.reset_refresh_state()
    # stale + remote gone -> falls back to the cached snapshot, no error
    assert policy.resolve_effective(project_root).source == "org"


def test_bind_fetches_and_caches_snapshot(home: Path, tmp_path: Path) -> None:
    # Rules live inline in policy.toml — a single self-contained org file.
    url = _make_policy_repo(
        tmp_path,
        'version = 1\nname = "acme"\n[artifacts]\nblocked_skills = ["a/foo"]\n'
        '[[rule]]\nid = "c1"\nseverity = "high"\nprompt = "p"\n',
    )
    resolved = policy.bind(url)
    assert resolved.source == "org"
    assert resolved.policy.blocked_skills == ["a/foo"]
    assert [r.id for r in resolved.policy.custom_rules] == ["c1"]
    assert policy.load_org_snapshot(url) is not None
    assert policy.org_snapshot_sha(url) is not None


def test_org_scope_resolves_from_aim_toml(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)  # warm cache
    _set_org(project_root, url)
    resolved = policy.resolve_effective(project_root)
    assert resolved.source == "org"
    assert resolved.policy.name == "acme"


def test_org_resolves_offline_after_remote_gone(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    import shutil

    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    shutil.rmtree(tmp_path / "polrepo.git")
    shutil.rmtree(policy._policy_clone_dir(url), ignore_errors=True)
    assert policy.resolve_effective(project_root).source == "org"  # from cached snapshot


def test_org_without_snapshot_fails_closed(home: Path, project_root: Path) -> None:
    # Unreachable file:// repo -> the opportunistic refresh fails fast (no network)
    # and, with no cached snapshot, resolution fails closed.
    _set_org(project_root, "file:///nonexistent/policy.git")
    policy.reset_refresh_state()
    with pytest.raises(policy.PolicyError, match="refresh"):
        policy.resolve_effective(project_root)
    policy.reset_refresh_state()
    with pytest.raises(policy.PolicyError):
        policy.effective_policy(project_root)


def test_corrupt_snapshot_self_heals_when_online(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import db
    from aim.core.models import GlobalSetting

    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    with db.session() as s:
        row = s.get(GlobalSetting, policy._org_snapshot_key(url))
        row.value = "{not json"
        s.commit()
    assert policy.load_org_snapshot(url) is None
    policy.reset_refresh_state()
    # corrupt is treated as expired -> opportunistic re-fetch heals it (remote is up)
    assert policy.resolve_effective(project_root).policy.name == "acme"


def test_corrupt_snapshot_offline_fails_closed(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    import shutil

    from aim.core import db
    from aim.core.models import GlobalSetting

    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    with db.session() as s:
        row = s.get(GlobalSetting, policy._org_snapshot_key(url))
        row.value = "{not json"
        s.commit()
    shutil.rmtree(tmp_path / "polrepo.git")
    shutil.rmtree(policy._policy_clone_dir(url), ignore_errors=True)
    policy.reset_refresh_state()
    with pytest.raises(policy.PolicyError):
        policy.resolve_effective(project_root)


def test_bind_rejects_insecure_http(home: Path) -> None:
    with pytest.raises(content_guard.InsecureTransportError):
        policy.bind("http://insecure/policy.git")


def test_unbind_returns_to_builtin(home: Path, project_root: Path, tmp_path: Path) -> None:
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    policy.bind(url)
    _set_org(project_root, url)
    assert policy.resolve_effective(project_root).source == "org"
    policy.set_project_policy(project_root, {})  # `aim policy unbind`
    assert policy.resolve_effective(project_root).source == "builtin"


def test_lock_records_org_repo_sha_hash(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    url = _make_policy_repo(tmp_path, 'version = 1\nname = "acme"\n')
    bound = policy.bind(url)
    _set_org(project_root, url)
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.policy_repo == url
    assert m.policy_ref == policy.org_snapshot_sha(url)
    assert m.policy_hash == bound.hash


def test_org_policy_blocks_skill_at_lock(home: Path, project_root: Path, tmp_path: Path) -> None:
    _setup_locked_skill_project(project_root, tmp_path)
    url = _make_policy_repo(
        tmp_path, 'version = 1\nname = "acme"\n[artifacts]\nblocked_skills = ["a/foo"]\n'
    )
    policy.bind(url)
    _set_org(project_root, url)
    with pytest.raises(policy.PolicyViolationError, match="a/foo"):
        asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))


# ---------------------------------------------------------------------------
# `aim policy validate` (the CI gate)
# ---------------------------------------------------------------------------


_runner = CliRunner()


def _validate(project_root: Path, *extra: str) -> int:
    result = _runner.invoke(cli.app, ["policy", "validate", str(project_root), *extra])
    return result.exit_code


def test_validate_blocks_disallowed(home: Path, project_root: Path) -> None:
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
    _set_local(project_root, policy.Policy(name="local", blocked_skills=["a/bad"]))
    assert _validate(project_root) == 1


def test_validate_ok_when_clean(home: Path, project_root: Path) -> None:
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
    _set_local(project_root, policy.Policy(name="local"))
    assert _validate(project_root) == 0


def test_validate_no_aim_toml_is_clean(home: Path, project_root: Path) -> None:
    assert _validate(project_root) == 0


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
    assert _validate(project_root, "--policy", url) == 1


def test_validate_remote_rejects_insecure_http(home: Path, project_root: Path) -> None:
    _write_declarations(project_root)
    assert _validate(project_root, "--policy", "http://insecure/policy.git") == 1


# ---------------------------------------------------------------------------
# migration & lockfile round-trip
# ---------------------------------------------------------------------------


def test_manifest_migration_adds_policy_fields() -> None:
    out = manifest_migrate.migrate({"manifest_version": 8, "skills": [], "rules": []})
    assert out["manifest_version"] == 15
    assert out["plugins"] == []
    assert "instruction_template" not in out
    assert "instruction_archetype" not in out
    assert out["policy_repo"] is None
    assert out["policy_ref"] is None
    assert out["policy_hash"] is None
    assert out["archetype"] is None
    assert out["template_repo"] is None
    assert out["template_qualified_name"] is None
    assert out["template_ref"] is None
    assert out["template_hash"] is None


def test_declarations_migration_v3_to_v4_adds_policy(home: Path, project_root: Path) -> None:
    import tomli_w

    (project_root / "aim.toml").write_text(
        tomli_w.dumps({"manifest_version": 3, "instruction_template": "default"})
    )
    decl = declarations.load(project_root)
    assert decl.manifest_version == 9
    assert decl.policy == {}
    assert decl.archetype.is_builtin
    assert decl.template is None
    assert decl.plugins == []


def test_lockfile_preserves_policy_fields(home: Path, project_root: Path) -> None:
    m = Manifest(policy_repo="https://example/p", policy_ref="deadbeef", policy_hash="cafe")
    manifest.save(project_root, m)
    loaded = manifest.load(project_root)
    assert loaded.policy_repo == "https://example/p"
    assert loaded.policy_ref == "deadbeef"
    assert loaded.policy_hash == "cafe"
