"""`aim lock` — resolve `aim.toml` declarations into an exact `aim.lock.toml`.

The resolver:
1. Reads `aim.toml`.
2. Ensures declared repos are registered (auto-register missing ones).
3. Resolves each declared skill/agent to a concrete SHA/version.
4. Fetches each declared MCP server's registry entry.
5. Computes content hashes and managed-region hashes for drift detection.
6. Writes `aim.lock.toml`.

This is the only command that mutates `aim.lock.toml` based on `aim.toml`.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aim.core import (
    agents,
    agents_md,
    declarations,
    git,
    hashing,
    layout_profiles,
    manifest,
    mcp_install,
    mcp_registry,
    repo_rules,
    repos,
    skills,
    templates,
)
from aim.core.models import (
    DeclaredAgent,
    DeclaredMcpServer,
    DeclaredRule,
    DeclaredSkill,
    InstalledAgent,
    InstalledMcpServer,
    InstalledRule,
    InstalledSkill,
    Manifest,
    ProjectDeclarations,
    RenderRule,
    SkillVersion,
)


class LockError(RuntimeError):
    """Top-level lock failure (missing aim.toml, unreachable repo, etc.)."""


@dataclass
class LockOptions:
    project_root: Path
    progress_callback: Callable[[str, str, str], object] | None = None
    allow_insecure: bool = False
    force: bool = False


@dataclass
class LockResult:
    project_root: Path
    locked_skills: list[str] = field(default_factory=list)
    locked_agents: list[str] = field(default_factory=list)
    locked_mcp: list[str] = field(default_factory=list)
    locked_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unchanged: bool = False


def _notify(
    callback: Callable[[str, str, str], object] | None, kind: str, name: str, status: str
) -> None:
    if callback is not None:
        try:
            callback(kind, name, status)
        except Exception:
            pass


def _load_declarations(project_root: Path) -> ProjectDeclarations:
    try:
        return declarations.load(project_root)
    except declarations.DeclarationsNotFoundError as exc:
        raise LockError(f"no aim.toml in {project_root}; run `aim init` first") from exc


def _resolve_profile(
    project_root: Path, decl: ProjectDeclarations
) -> layout_profiles.LayoutProfile:
    if decl.layout_profile:
        try:
            return layout_profiles.get_profile(project_root, decl.layout_profile)
        except layout_profiles.LayoutProfileNotFoundError:
            return layout_profiles.BUILTIN_CLAUDE
    return layout_profiles.BUILTIN_CLAUDE


def _ensure_repo(alias: str, url: str, allow_insecure: bool) -> str | None:
    try:
        repos.get(alias)
        skills.index_repo(alias)
        agents.index_repo(alias)
        return None
    except repos.RepoNotFoundError:
        pass
    try:
        repos.add(alias, url, allow_empty=True, allow_insecure=allow_insecure)
    except Exception as exc:
        return f"repo {alias}: failed to register {url}: {exc}"
    return None


async def _ensure_repos(decl: ProjectDeclarations, allow_insecure: bool) -> list[str]:
    pairs = {alias: url for alias, url in decl.repos.items()}
    if not pairs:
        return []

    async def _one(alias: str, url: str) -> str | None:
        return await asyncio.to_thread(_ensure_repo, alias, url, allow_insecure)

    results = await asyncio.gather(*(_one(alias, url) for alias, url in pairs.items()))
    return [r for r in results if r is not None]


def _resolve_skill_version(skill: DeclaredSkill) -> SkillVersion:
    repo_dir = repos.clone_dir(skill.repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, skill.pin or skill.track or "HEAD")
    return SkillVersion(
        tag=skill.pin,
        sha=sha,
        installed_at=datetime.now(UTC),
    )


def _hash_skill_at_sha(skill: DeclaredSkill, sha: str) -> str:
    repo_dir = repos.clone_dir(skill.repo_alias)
    paths_in_tree = git.get_backend().ls_tree(repo_dir, sha, skill.source_path)
    h = hashlib.sha256()
    for rel_path in sorted(paths_in_tree):
        content = git.get_backend().cat_file_bytes(repo_dir, sha, rel_path)
        rel_under_source = rel_path[len(skill.source_path) + 1 :] if skill.source_path else rel_path
        h.update(rel_under_source.encode("utf-8"))
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()


def _lock_skill(
    skill: DeclaredSkill, cached: InstalledSkill | None = None
) -> tuple[InstalledSkill | None, str | None]:
    try:
        version = _resolve_skill_version(skill)
        if (
            cached is not None
            and cached.current.sha == version.sha
            and cached.source_path == skill.source_path
        ):
            content_hash = cached.content_hash
        else:
            content_hash = _hash_skill_at_sha(skill, version.sha)
    except Exception as exc:
        return None, f"{skill.qualified_name}: {exc}"
    repo_url = repos.get(skill.repo_alias).url
    installed = InstalledSkill(
        qualified_name=skill.qualified_name,
        repo_alias=skill.repo_alias,
        repo_url=repo_url,
        source_path=skill.source_path,
        target_dir=skill.target_dir,
        current=version,
        content_hash=content_hash,
        pin=skill.pin,
        track=skill.track,
    )
    return installed, None


async def _lock_skills(
    skills: list[DeclaredSkill],
    callback: Callable[[str, str, str], object] | None,
    cached_by_name: dict[str, InstalledSkill] | None = None,
) -> tuple[list[InstalledSkill], list[str]]:
    if not skills:
        return [], []

    lookup = cached_by_name or {}

    async def _one(skill: DeclaredSkill) -> tuple[InstalledSkill | None, str | None]:
        _notify(callback, "skill", skill.qualified_name, "locking")
        installed, error = await asyncio.to_thread(
            _lock_skill, skill, lookup.get(skill.qualified_name)
        )
        if error:
            _notify(callback, "skill", skill.qualified_name, "error")
        else:
            _notify(callback, "skill", skill.qualified_name, "ok")
        return installed, error

    results = await asyncio.gather(*(_one(s) for s in skills))
    locked = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return locked, errors


def _resolve_agent_version(agent: DeclaredAgent) -> SkillVersion:
    repo_dir = repos.clone_dir(agent.repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, agent.pin or agent.track or "HEAD")
    return SkillVersion(
        tag=agent.pin,
        sha=sha,
        installed_at=datetime.now(UTC),
    )


def _read_agent_at_sha(agent: DeclaredAgent, sha: str) -> str:
    repo_dir = repos.clone_dir(agent.repo_alias)
    if agent.source_path.endswith(".md"):
        artifact_path = agent.source_path
    else:
        artifact_path = f"{agent.source_path}/AGENT.md"
    return git.get_backend().cat_file(repo_dir, sha, artifact_path)


def _lock_agent(
    agent: DeclaredAgent, cached: InstalledAgent | None = None
) -> tuple[InstalledAgent | None, str | None]:
    try:
        version = _resolve_agent_version(agent)
        if (
            cached is not None
            and cached.current.sha == version.sha
            and cached.source_path == agent.source_path
        ):
            content_hash = cached.content_hash
        else:
            content = _read_agent_at_sha(agent, version.sha)
            content_hash = hashing.hash_text(content)
    except Exception as exc:
        return None, f"{agent.qualified_name}: {exc}"
    repo_url = repos.get(agent.repo_alias).url
    installed = InstalledAgent(
        qualified_name=agent.qualified_name,
        repo_alias=agent.repo_alias,
        repo_url=repo_url,
        source_path=agent.source_path,
        target_path=agent.target_path,
        current=version,
        content_hash=content_hash,
        pin=agent.pin,
        track=agent.track,
    )
    return installed, None


async def _lock_agents(
    agents: list[DeclaredAgent],
    callback: Callable[[str, str, str], object] | None,
    cached_by_name: dict[str, InstalledAgent] | None = None,
) -> tuple[list[InstalledAgent], list[str]]:
    if not agents:
        return [], []

    lookup = cached_by_name or {}

    async def _one(agent: DeclaredAgent) -> tuple[InstalledAgent | None, str | None]:
        _notify(callback, "agent", agent.qualified_name, "locking")
        installed, error = await asyncio.to_thread(
            _lock_agent, agent, lookup.get(agent.qualified_name)
        )
        if error:
            _notify(callback, "agent", agent.qualified_name, "error")
        else:
            _notify(callback, "agent", agent.qualified_name, "ok")
        return installed, error

    results = await asyncio.gather(*(_one(a) for a in agents))
    locked = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return locked, errors


def _resolve_rule_version(rule: DeclaredRule) -> SkillVersion:
    repo_dir = repos.clone_dir(rule.repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, rule.pin or rule.track or "HEAD")
    return SkillVersion(
        tag=rule.pin,
        sha=sha,
        installed_at=datetime.now(UTC),
    )


def _read_rule_at_sha(rule: DeclaredRule, sha: str) -> str:
    repo_dir = repos.clone_dir(rule.repo_alias)
    return git.get_backend().cat_file(repo_dir, sha, rule.source_path)


def _lock_rule(
    rule: DeclaredRule, cached: InstalledRule | None = None
) -> tuple[InstalledRule | None, str | None]:
    try:
        version = _resolve_rule_version(rule)
        if (
            cached is not None
            and cached.current.sha == version.sha
            and cached.source_path == rule.source_path
        ):
            content_hash = cached.content_hash
        else:
            content = _read_rule_at_sha(rule, version.sha)
            content_hash = hashing.hash_text(content)
    except Exception as exc:
        return None, f"{rule.qualified_name}: {exc}"
    repo_url = repos.get(rule.repo_alias).url
    installed = InstalledRule(
        qualified_name=rule.qualified_name,
        repo_alias=rule.repo_alias,
        repo_url=repo_url,
        source_path=rule.source_path,
        current=version,
        content_hash=content_hash,
        pin=rule.pin,
        track=rule.track,
    )
    return installed, None


async def _lock_rules(
    rules: list[DeclaredRule],
    callback: Callable[[str, str, str], object] | None,
    cached_by_name: dict[str, InstalledRule] | None = None,
) -> tuple[list[InstalledRule], list[str]]:
    if not rules:
        return [], []

    lookup = cached_by_name or {}

    async def _one(rule: DeclaredRule) -> tuple[InstalledRule | None, str | None]:
        _notify(callback, "rule", rule.qualified_name, "locking")
        installed, error = await asyncio.to_thread(
            _lock_rule, rule, lookup.get(rule.qualified_name)
        )
        if error:
            _notify(callback, "rule", rule.qualified_name, "error")
        else:
            _notify(callback, "rule", rule.qualified_name, "ok")
        return installed, error

    results = await asyncio.gather(*(_one(r) for r in rules))
    locked = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return locked, errors


def _lock_mcp(mcp: DeclaredMcpServer) -> tuple[InstalledMcpServer | None, str | None]:
    try:
        server = mcp_registry.find_server(mcp.registry_name, exact_name=mcp.registry_name)
        entry = mcp_registry.map_to_claude_entry(
            server, preferred_transport=mcp.preferred_transport
        )
        if mcp.overrides:
            entry = mcp_install._apply_overrides(entry, mcp.overrides)
        version = mcp_registry.make_mcp_server_version(server, entry=entry)
    except Exception as exc:
        return None, f"{mcp.alias}: {exc}"
    installed = InstalledMcpServer(
        alias=mcp.alias,
        registry_name=mcp.registry_name,
        entry=entry,
        entry_hash=mcp_registry.hash_entry(entry),
        current=version,
    )
    return installed, None


async def _lock_mcps(
    mcps: list[DeclaredMcpServer],
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[InstalledMcpServer], list[str]]:
    if not mcps:
        return [], []

    async def _one(mcp: DeclaredMcpServer) -> tuple[InstalledMcpServer | None, str | None]:
        _notify(callback, "mcp", mcp.alias, "locking")
        installed, error = await asyncio.to_thread(_lock_mcp, mcp)
        if error:
            _notify(callback, "mcp", mcp.alias, "error")
        else:
            _notify(callback, "mcp", mcp.alias, "ok")
        return installed, error

    results = await asyncio.gather(*(_one(m) for m in mcps))
    locked = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return locked, errors


def _compute_region_hashes(
    decl: ProjectDeclarations,
    profile: layout_profiles.LayoutProfile,
    locked_rules: list[InstalledRule],
) -> dict[str, str]:
    """Compute hashes for the rendered AGENTS.md regions from the locked rule
    bodies (read at their pinned SHAs) and the template."""
    applied: list[RenderRule] = [repo_rules.render_rule(r) for r in locked_rules]

    def _render_for_agent(agent: str | None) -> str:
        return templates.render(
            decl.instruction_template,
            {
                "rules": applied,
                "agent": agent,
                "rules_mode": profile.rules_mode,
                "rules_dir": profile.rules_dir,
            },
        )

    canonical = _render_for_agent(None)
    regions = {r.name: r.body for r in agents_md.parse(canonical)}
    return {name: hashing.hash_text(body) for name, body in regions.items()}


def _skill_key(s: InstalledSkill) -> tuple:
    return (
        s.qualified_name,
        s.repo_alias,
        s.repo_url,
        s.source_path,
        s.target_dir,
        s.content_hash,
        s.current.sha,
        s.current.tag,
        s.pin,
        s.track,
    )


def _agent_key(a: InstalledAgent) -> tuple:
    return (
        a.qualified_name,
        a.repo_alias,
        a.repo_url,
        a.source_path,
        a.target_path,
        a.content_hash,
        a.current.sha,
        a.current.tag,
        a.pin,
        a.track,
    )


def _rule_key(r: InstalledRule) -> tuple:
    return (
        r.qualified_name,
        r.repo_alias,
        r.repo_url,
        r.source_path,
        r.content_hash,
        r.current.sha,
        r.current.tag,
        r.pin,
        r.track,
    )


def _mcp_key(m: InstalledMcpServer) -> tuple:
    return (
        m.alias,
        m.registry_name,
        m.entry_hash,
        m.current.definition_hash,
        m.current.registry_version,
        m.current.overrides,
        m.overrides,
    )


def _top_level_key(m: Manifest) -> tuple:
    return (
        m.instruction_template,
        m.layout_profile,
        tuple(m.symlinks),
        tuple(m.managed_files),
        tuple(sorted(m.managed_region_hashes.items())),
    )


def _lockfile_unchanged(existing: Manifest, new: Manifest) -> bool:
    if _top_level_key(existing) != _top_level_key(new):
        return False
    if [_skill_key(s) for s in existing.skills] != [_skill_key(s) for s in new.skills]:
        return False
    if [_agent_key(a) for a in existing.agents] != [_agent_key(a) for a in new.agents]:
        return False
    if [_mcp_key(m) for m in existing.mcp_servers] != [_mcp_key(m) for m in new.mcp_servers]:
        return False
    if [_rule_key(r) for r in existing.rules] != [_rule_key(r) for r in new.rules]:
        return False
    return True


def _preserve_unchanged_metadata(existing: Manifest | None, new: Manifest) -> None:
    """For items whose comparison key matches an entry in `existing`, restore the
    prior `installed_at` and `history` so unchanged items don't show a fresh
    timestamp (and don't lose rollback history) on a partial-change re-lock.
    """
    if existing is None:
        return

    skill_by_key = {_skill_key(s): s for s in existing.skills}
    for s in new.skills:
        prev_skill = skill_by_key.get(_skill_key(s))
        if prev_skill is not None:
            s.current = prev_skill.current
            s.history = list(prev_skill.history)

    agent_by_key = {_agent_key(a): a for a in existing.agents}
    for a in new.agents:
        prev_agent = agent_by_key.get(_agent_key(a))
        if prev_agent is not None:
            a.current = prev_agent.current
            a.history = list(prev_agent.history)

    mcp_by_key = {_mcp_key(m): m for m in existing.mcp_servers}
    for m in new.mcp_servers:
        prev_mcp = mcp_by_key.get(_mcp_key(m))
        if prev_mcp is not None:
            m.current = prev_mcp.current
            m.history = list(prev_mcp.history)

    rule_by_key = {_rule_key(r): r for r in existing.rules}
    for r in new.rules:
        prev_rule = rule_by_key.get(_rule_key(r))
        if prev_rule is not None:
            r.current = prev_rule.current
            r.history = list(prev_rule.history)


async def run(options: LockOptions) -> LockResult:
    project_root = options.project_root.resolve()
    decl = _load_declarations(project_root)
    profile = _resolve_profile(project_root, decl)

    result = LockResult(project_root=project_root)

    try:
        existing = manifest.load(project_root)
    except manifest.ManifestNotFoundError:
        existing = None

    cached_skills: dict[str, InstalledSkill] = (
        {}
        if options.force
        else {s.qualified_name: s for s in (existing.skills if existing else [])}
    )
    cached_agents: dict[str, InstalledAgent] = (
        {}
        if options.force
        else {a.qualified_name: a for a in (existing.agents if existing else [])}
    )
    cached_rules: dict[str, InstalledRule] = (
        {} if options.force else {r.qualified_name: r for r in (existing.rules if existing else [])}
    )

    _notify(options.progress_callback, "repos", "all", "locking")
    result.errors = await _ensure_repos(decl, options.allow_insecure)
    _notify(options.progress_callback, "repos", "all", "ok")

    # Skills, agents, MCPs, and rules only depend on repos being available, so
    # lock them concurrently instead of sequentially.
    skills_task = _lock_skills(decl.skills, options.progress_callback, cached_skills)
    agents_task = _lock_agents(decl.agents, options.progress_callback, cached_agents)
    mcps_task = _lock_mcps(decl.mcp_servers, options.progress_callback)
    rules_task = _lock_rules(decl.rules, options.progress_callback, cached_rules)

    (
        (skills_locked, skill_errors),
        (agents_locked, agent_errors),
        (mcps_locked, mcp_errors),
        (rules_locked, rule_errors),
    ) = await asyncio.gather(skills_task, agents_task, mcps_task, rules_task)

    result.locked_skills = [s.qualified_name for s in skills_locked]
    result.locked_agents = [a.qualified_name for a in agents_locked]
    result.locked_mcp = [m.alias for m in mcps_locked]
    result.locked_rules = [r.qualified_name for r in rules_locked]

    # Region hashes depend on the locked rule bodies (read at their pinned
    # SHAs), so compute them after the rule lock — not concurrently — to avoid a
    # moving-HEAD race between the region hash and each rule's content_hash.
    region_hashes = await asyncio.to_thread(_compute_region_hashes, decl, profile, rules_locked)

    managed_files = [
        profile.agents_md,
        *decl.symlinks,
    ]

    lock = Manifest(
        instruction_template=decl.instruction_template,
        layout_profile=decl.layout_profile or profile.name,
        rules=rules_locked,
        symlinks=decl.symlinks,
        managed_files=list(dict.fromkeys(managed_files)),
        managed_region_hashes=region_hashes,
        skills=skills_locked,
        agents=agents_locked,
        mcp_servers=mcps_locked,
    )

    if not options.force:
        _preserve_unchanged_metadata(existing, lock)

    all_errors = result.errors + skill_errors + agent_errors + mcp_errors + rule_errors
    if (
        existing is not None
        and not options.force
        and not all_errors
        and _lockfile_unchanged(existing, lock)
    ):
        result.unchanged = True
        return result

    manifest.save(project_root, lock)

    if all_errors:
        # Lock is partial; re-run will continue where it left off.
        raise LockError("; ".join(all_errors))

    return result
