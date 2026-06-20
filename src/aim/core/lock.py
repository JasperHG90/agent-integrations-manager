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
import threading
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
    policy,
    repo_rules,
    repos,
    skills,
    templates,
)
from aim.core.models import (
    DeclaredAgent,
    DeclaredArchetype,
    DeclaredMcpServer,
    DeclaredRule,
    DeclaredSkill,
    InstalledAgent,
    InstalledArchetype,
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
    """Inputs controlling a single lock run."""

    project_root: Path
    progress_callback: Callable[[str, str, str], object] | None = None
    allow_insecure: bool = False
    force: bool = False


@dataclass
class LockResult:
    """Outcome of a lock run: which artifacts were locked, plus warnings/errors."""

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
    """Invoke the progress callback, swallowing any exception it raises.

    Args:
        callback: Optional progress sink; a no-op when None.
        kind: Artifact category (e.g. "skill", "agent", "mcp", "rule").
        name: Identifier of the artifact being reported on.
        status: Lifecycle status (e.g. "locking", "ok", "error").
    """
    if callback is not None:
        try:
            callback(kind, name, status)
        except Exception:
            pass


def _load_declarations(project_root: Path) -> ProjectDeclarations:
    """Load `aim.toml` declarations for the project.

    Raises:
        LockError: if no `aim.toml` exists at the project root.
    """
    try:
        return declarations.load(project_root)
    except declarations.DeclarationsNotFoundError as exc:
        raise LockError(f"no aim.toml in {project_root}; run `aim init` first") from exc


def _resolve_profile(
    project_root: Path, decl: ProjectDeclarations
) -> layout_profiles.LayoutProfile:
    """Resolve the declared layout profile, falling back to the built-in default.

    Args:
        project_root: Project root used to locate custom profile definitions.
        decl: Loaded project declarations whose `layout_profile` is honored.

    Returns:
        The named profile, or `BUILTIN_CLAUDE` when none is declared or it is missing.
    """
    if decl.layout_profile:
        try:
            return layout_profiles.get_profile(project_root, decl.layout_profile)
        except layout_profiles.LayoutProfileNotFoundError:
            return layout_profiles.BUILTIN_CLAUDE
    return layout_profiles.BUILTIN_CLAUDE


def _ensure_repo(alias: str, url: str, allow_insecure: bool) -> str | None:
    """Ensure a declared repo is registered, auto-registering it if missing.

    Args:
        alias: Repo alias as referenced by declarations.
        url: Clone URL to register the repo under when not already present.
        allow_insecure: Permit insecure transports when registering.

    Returns:
        None on success, or an error message string if registration failed.
    """
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
    """Register all declared repos concurrently.

    Args:
        decl: Project declarations whose `repos` mapping is registered.
        allow_insecure: Permit insecure transports when registering.

    Returns:
        A list of per-repo error messages; empty if every repo registered cleanly.
    """
    pairs = {alias: url for alias, url in decl.repos.items()}
    if not pairs:
        return []

    async def _one(alias: str, url: str) -> str | None:
        return await asyncio.to_thread(_ensure_repo, alias, url, allow_insecure)

    results = await asyncio.gather(*(_one(alias, url) for alias, url in pairs.items()))
    return [r for r in results if r is not None]


# Refs are stable for the duration of one lock run (repos are fetched once, up
# front, in `_ensure_repos`). Many artifacts in the same repo share a ref —
# commonly "HEAD" — so resolving each independently spawns one redundant
# `git rev-parse` per artifact. This per-run cache collapses those to one spawn
# per unique (repo, ref). It's a module global so the `asyncio.to_thread` worker
# threads share it; `run()` clears it at the start of every lock to avoid stale
# SHAs leaking across runs in a long-lived process (TUI/tests).
_ref_cache: dict[tuple[str, str], str] = {}
_ref_cache_lock = threading.Lock()


def _resolve_ref_cached(repo_dir: Path, ref: str) -> str:
    """Resolve a git ref to a SHA, caching the result for the current lock run.

    Args:
        repo_dir: Path to the cloned repo to resolve within.
        ref: Ref to resolve (branch, tag, or "HEAD").

    Returns:
        The resolved commit SHA.
    """
    key = (str(repo_dir), ref)
    with _ref_cache_lock:
        sha = _ref_cache.get(key)
    if sha is not None:
        return sha
    sha = git.get_backend().resolve_ref(repo_dir, ref)
    with _ref_cache_lock:
        _ref_cache[key] = sha
    return sha


def _resolve_skill_version(skill: DeclaredSkill) -> SkillVersion:
    """Resolve a declared skill's pin/track to a concrete SkillVersion."""
    repo_dir = repos.clone_dir(skill.repo_alias)
    sha = _resolve_ref_cached(repo_dir, skill.pin or skill.track or "HEAD")
    return SkillVersion(
        tag=skill.pin,
        sha=sha,
        installed_at=datetime.now(UTC),
    )


def _hash_skill_at_sha(skill: DeclaredSkill, sha: str) -> str:
    """Compute a stable content hash of a skill's tree at a given SHA.

    Args:
        skill: Declared skill whose `source_path` subtree is hashed.
        sha: Commit SHA to read the tree at.

    Returns:
        A hex SHA-256 digest over the source-relative paths and blob contents.
    """
    repo_dir = repos.clone_dir(skill.repo_alias)
    paths_in_tree = sorted(git.get_backend().ls_tree(repo_dir, sha, skill.source_path))
    blobs = git.get_backend().cat_file_batch(repo_dir, sha, paths_in_tree)
    h = hashlib.sha256()
    for rel_path in paths_in_tree:
        content = blobs[rel_path]
        rel_under_source = rel_path[len(skill.source_path) + 1 :] if skill.source_path else rel_path
        h.update(rel_under_source.encode("utf-8"))
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()


def _lock_skill(
    skill: DeclaredSkill, cached: InstalledSkill | None = None
) -> tuple[InstalledSkill | None, str | None]:
    """Lock a single declared skill to a concrete InstalledSkill.

    Reuses the cached content hash when the resolved SHA and source path are
    unchanged, avoiding a redundant tree hash.

    Args:
        skill: Declared skill to resolve and hash.
        cached: Prior installed skill from an existing lockfile, if any.

    Returns:
        A `(InstalledSkill, None)` pair on success, or `(None, error message)`.
    """
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
    """Lock all declared skills concurrently, reporting progress per skill.

    Args:
        skills: Declared skills to lock.
        callback: Optional progress sink notified as each skill is locked.
        cached_by_name: Prior installed skills keyed by qualified name, for hash reuse.

    Returns:
        A `(locked skills, error messages)` pair.
    """
    if not skills:
        return [], []

    lookup = cached_by_name or {}

    async def _one(skill: DeclaredSkill) -> tuple[InstalledSkill | None, str | None]:
        """Lock one skill off-thread and emit progress notifications."""
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
    """Resolve a declared agent's pin/track to a concrete SkillVersion."""
    repo_dir = repos.clone_dir(agent.repo_alias)
    sha = _resolve_ref_cached(repo_dir, agent.pin or agent.track or "HEAD")
    return SkillVersion(
        tag=agent.pin,
        sha=sha,
        installed_at=datetime.now(UTC),
    )


def _read_agent_at_sha(agent: DeclaredAgent, sha: str) -> str:
    """Read an agent's artifact body at a given SHA.

    Args:
        agent: Declared agent whose source path locates the artifact.
        sha: Commit SHA to read at.

    Returns:
        The artifact text; for a directory source path, reads its `AGENT.md`.
    """
    repo_dir = repos.clone_dir(agent.repo_alias)
    if agent.source_path.endswith(".md"):
        artifact_path = agent.source_path
    else:
        artifact_path = f"{agent.source_path}/AGENT.md"
    return git.get_backend().cat_file(repo_dir, sha, artifact_path)


def _lock_agent(
    agent: DeclaredAgent, cached: InstalledAgent | None = None
) -> tuple[InstalledAgent | None, str | None]:
    """Lock a single declared agent to a concrete InstalledAgent.

    Reuses the cached content hash when the resolved SHA and source path are
    unchanged.

    Args:
        agent: Declared agent to resolve and hash.
        cached: Prior installed agent from an existing lockfile, if any.

    Returns:
        An `(InstalledAgent, None)` pair on success, or `(None, error message)`.
    """
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
    """Lock all declared agents concurrently, reporting progress per agent.

    Args:
        agents: Declared agents to lock.
        callback: Optional progress sink notified as each agent is locked.
        cached_by_name: Prior installed agents keyed by qualified name, for hash reuse.

    Returns:
        A `(locked agents, error messages)` pair.
    """
    if not agents:
        return [], []

    lookup = cached_by_name or {}

    async def _one(agent: DeclaredAgent) -> tuple[InstalledAgent | None, str | None]:
        """Lock one agent off-thread and emit progress notifications."""
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
    """Resolve a declared rule's pin/track to a concrete SkillVersion."""
    repo_dir = repos.clone_dir(rule.repo_alias)
    sha = _resolve_ref_cached(repo_dir, rule.pin or rule.track or "HEAD")
    return SkillVersion(
        tag=rule.pin,
        sha=sha,
        installed_at=datetime.now(UTC),
    )


def _read_rule_at_sha(rule: DeclaredRule, sha: str) -> str:
    """Read a rule's body text at a given SHA."""
    repo_dir = repos.clone_dir(rule.repo_alias)
    return git.get_backend().cat_file(repo_dir, sha, rule.source_path)


def _resolve_archetype_version(declared: DeclaredArchetype) -> SkillVersion:
    """Resolve a declared archetype's pin/track to a concrete SkillVersion."""
    repo_dir = repos.clone_dir(declared.repo_alias)
    sha = _resolve_ref_cached(repo_dir, declared.pin or declared.track or "HEAD")
    return SkillVersion(tag=declared.pin, sha=sha, installed_at=datetime.now(UTC))


def _lock_archetype(
    declared: DeclaredArchetype, cached: InstalledArchetype | None = None
) -> tuple[InstalledArchetype | None, str | None]:
    """Lock the declared instruction archetype to a concrete InstalledArchetype.

    Args:
        declared: Declared archetype to resolve and hash.
        cached: Prior installed archetype from an existing lockfile, if any.

    Returns:
        An `(InstalledArchetype, None)` pair on success, or `(None, error message)`.
    """
    try:
        version = _resolve_archetype_version(declared)
        if (
            cached is not None
            and cached.current.sha == version.sha
            and cached.source_path == declared.source_path
        ):
            content_hash = cached.content_hash
        else:
            repo_dir = repos.clone_dir(declared.repo_alias)
            content = git.get_backend().cat_file(repo_dir, version.sha, declared.source_path)
            content_hash = hashing.hash_text(content)
    except Exception as exc:
        return None, f"{declared.qualified_name}: {exc}"
    return (
        InstalledArchetype(
            qualified_name=declared.qualified_name,
            repo_alias=declared.repo_alias,
            repo_url=repos.get(declared.repo_alias).url,
            source_path=declared.source_path,
            current=version,
            content_hash=content_hash,
            pin=declared.pin,
            track=declared.track,
        ),
        None,
    )


def _lock_rule(
    rule: DeclaredRule, cached: InstalledRule | None = None
) -> tuple[InstalledRule | None, str | None]:
    """Lock a single declared rule to a concrete InstalledRule.

    Reuses the cached content hash when the resolved SHA and source path are
    unchanged.

    Args:
        rule: Declared rule to resolve and hash.
        cached: Prior installed rule from an existing lockfile, if any.

    Returns:
        An `(InstalledRule, None)` pair on success, or `(None, error message)`.
    """
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
    """Lock all declared rules concurrently, reporting progress per rule.

    Args:
        rules: Declared rules to lock.
        callback: Optional progress sink notified as each rule is locked.
        cached_by_name: Prior installed rules keyed by qualified name, for hash reuse.

    Returns:
        A `(locked rules, error messages)` pair.
    """
    if not rules:
        return [], []

    lookup = cached_by_name or {}

    async def _one(rule: DeclaredRule) -> tuple[InstalledRule | None, str | None]:
        """Lock one rule off-thread and emit progress notifications."""
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
    """Lock a single declared MCP server from its registry entry.

    Args:
        mcp: Declared MCP server to resolve against the registry.

    Returns:
        An `(InstalledMcpServer, None)` pair on success, or `(None, error message)`.
    """
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
    """Lock all declared MCP servers concurrently, reporting progress per server.

    Args:
        mcps: Declared MCP servers to lock.
        callback: Optional progress sink notified as each server is locked.

    Returns:
        A `(locked MCP servers, error messages)` pair.
    """
    if not mcps:
        return [], []

    async def _one(mcp: DeclaredMcpServer) -> tuple[InstalledMcpServer | None, str | None]:
        """Lock one MCP server off-thread and emit progress notifications."""
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
    """Compute per-region hashes of the rendered AGENTS.md for drift detection.

    The regions come from rendering the instruction template over the locked rule
    bodies read at their pinned SHAs.

    Args:
        decl: Project declarations supplying the instruction template.
        profile: Layout profile controlling rules mode and directory.
        locked_rules: Rules already resolved to pinned SHAs.

    Returns:
        A mapping of region name to the hash of that region's body.
    """
    applied: list[RenderRule] = [repo_rules.render_rule(r) for r in locked_rules]

    def _render_for_agent(agent: str | None) -> str:
        """Render the instruction template for one agent (or the canonical None)."""
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
    # With an archetype the base prose comes from the archetype, not the template, so
    # only aim's dynamic `rules` region is managed inside AGENTS.md (matches the render).
    if decl.instruction_archetype is not None:
        regions = {"rules": regions["rules"]} if "rules" in regions else {}
    return {name: hashing.hash_text(body) for name, body in regions.items()}


def _skill_key(s: InstalledSkill) -> tuple:
    """Build the identity tuple used to detect whether a locked skill changed."""
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
    """Build the identity tuple used to detect whether a locked agent changed."""
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
    """Build the identity tuple used to detect whether a locked rule changed."""
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
    """Build the identity tuple used to detect whether a locked MCP server changed."""
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
    """Build the identity tuple for a manifest's non-artifact (top-level) fields."""
    return (
        m.instruction_template,
        m.layout_profile,
        tuple(m.symlinks),
        tuple(m.managed_files),
        tuple(sorted(m.managed_region_hashes.items())),
        m.policy_repo,
        m.policy_ref,
        m.policy_hash,
    )


def _lockfile_unchanged(existing: Manifest, new: Manifest) -> bool:
    """Report whether two manifests are equivalent across all identity keys.

    Args:
        existing: Manifest currently on disk.
        new: Manifest just computed by this lock run.

    Returns:
        True if every top-level field and artifact list matches.
    """
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
    """Restore prior version metadata for artifacts unchanged since the last lock.

    For each new item whose comparison key matches an existing entry, the prior
    `current` (with its `installed_at`) and `history` are copied over, so unchanged
    items keep their original timestamp and rollback history on a partial re-lock.

    Args:
        existing: Manifest from a previous lock, or None on a first lock.
        new: Manifest mutated in place to carry forward preserved metadata.
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


def _enforce_policy(
    decl: ProjectDeclarations, resolved: policy.ResolvedPolicy, *, effective_profile: str
) -> None:
    """Refuse to lock declarations that the governing policy disallows.

    Args:
        decl: Project declarations to validate against policy.
        resolved: The effective resolved policy.
        effective_profile: Layout profile name the lockfile will record.

    Raises:
        Exception: propagated from policy assertions when something is disallowed.
    """
    pol = resolved.policy
    for alias, url in decl.repos.items():
        policy.assert_repo_allowed(pol, alias, url)
    for s in decl.skills:
        policy.assert_artifact_allowed(pol, "skill", s.qualified_name)
    for a in decl.agents:
        policy.assert_artifact_allowed(pol, "agent", a.qualified_name)
    for r in decl.rules:
        policy.assert_artifact_allowed(pol, "rule", r.qualified_name)
    for mcp in decl.mcp_servers:
        policy.assert_mcp_allowed(pol, mcp.alias, mcp.registry_name)
    if decl.instruction_archetype is not None:
        policy.assert_archetype_allowed(pol, decl.instruction_archetype.qualified_name)
    # Check the EFFECTIVE profile (the one the lockfile records), never the raw
    # possibly-None declaration, so an allow-list isn't bypassed by relying on the default.
    policy.assert_profile_allowed(pol, effective_profile)


async def run(options: LockOptions) -> LockResult:
    """Resolve `aim.toml` into `aim.lock.toml`, writing the lockfile if it changed.

    Args:
        options: Lock inputs (project root, progress callback, force, insecure).

    Returns:
        A LockResult summarizing locked artifacts, warnings, and errors. Its
        `unchanged` flag is set when an existing lockfile already matches.

    Raises:
        LockError: if `aim.toml` is missing, policy disallows a declaration, or
            the lock completes only partially (some artifacts failed to resolve).
    """
    with _ref_cache_lock:
        _ref_cache.clear()
    project_root = options.project_root.resolve()
    decl = _load_declarations(project_root)
    profile = _resolve_profile(project_root, decl)

    resolved_policy = policy.resolve_effective(project_root)
    _enforce_policy(decl, resolved_policy, effective_profile=decl.layout_profile or profile.name)

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

    archetype_locked: InstalledArchetype | None = None
    archetype_errors: list[str] = []
    if decl.instruction_archetype is not None:
        cached_archetype = (
            None if options.force or existing is None else existing.instruction_archetype
        )
        archetype_locked, archetype_error = _lock_archetype(
            decl.instruction_archetype, cached_archetype
        )
        if archetype_error is not None:
            archetype_errors.append(archetype_error)

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
        instruction_archetype=archetype_locked,
        layout_profile=decl.layout_profile or profile.name,
        rules=rules_locked,
        symlinks=decl.symlinks,
        managed_files=list(dict.fromkeys(managed_files)),
        managed_region_hashes=region_hashes,
        skills=skills_locked,
        agents=agents_locked,
        mcp_servers=mcps_locked,
        policy_repo=resolved_policy.repo,
        policy_ref=(
            policy.org_snapshot_sha(resolved_policy.repo)
            if resolved_policy.source == "org" and resolved_policy.repo
            else None
        ),
        policy_hash=resolved_policy.hash,
    )

    if not options.force:
        _preserve_unchanged_metadata(existing, lock)

    all_errors = (
        result.errors + skill_errors + agent_errors + mcp_errors + rule_errors + archetype_errors
    )
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
