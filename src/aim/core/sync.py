"""`aim sync` — reproduce the committed project state from `aim.lock.toml`.

This is the package-manager-style reconciliation engine: read the lockfile,
ensure source repos are registered and indexed, then restore rules, skills,
agents, MCP servers, and agent instruction files to the exact versions stored.

Operations that block on git/network/filesystem are offloaded to
`asyncio.to_thread()`; manifest/DB writes stay on the async main thread to
avoid SQLite concurrency issues.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aim.core import (
    agent_files,
    agents,
    content_guard,
    git,
    hashing,
    layout_profiles,
    manifest,
    mcp_install,
    mcp_registry,
    paths,
    repos,
    skills,
)
from aim.core import (
    install as install_mod,
)
from aim.core import (
    rules as rules_mod,
)
from aim.core.models import InstalledAgent, InstalledMcpServer, InstalledSkill, Manifest


class SyncError(RuntimeError):
    """Top-level sync failure (missing lockfile, unreachable repo, etc.)."""


class SyncDriftError(RuntimeError):
    """A managed artifact was edited locally and `force=False`."""


class SyncRepoError(RuntimeError):
    """A source repo could not be registered or indexed."""


@dataclass
class SyncOptions:
    project_root: Path
    force: bool = False
    sync_agents: bool = True
    layout_profile: str | None = None
    progress_callback: Callable[[str, str, str], object] | None = None
    allow_insecure: bool = False


@dataclass
class SyncResult:
    project_root: Path
    synced_skills: list[str] = field(default_factory=list)
    synced_agents: list[str] = field(default_factory=list)
    synced_mcp: list[str] = field(default_factory=list)
    drift_warnings: list[str] = field(default_factory=list)
    repo_errors: list[str] = field(default_factory=list)
    rules_applied: list[str] = field(default_factory=list)


def _notify(callback: Callable[[str, str, str], object] | None, kind: str, name: str, status: str) -> None:
    if callback is not None:
        try:
            callback(kind, name, status)
        except Exception:
            pass


def _load_lock(project_root: Path) -> Manifest:
    try:
        return manifest.load(project_root)
    except manifest.ManifestNotFoundError as exc:
        raise SyncError(
            f"no aim.lock.toml in {project_root}; run `aim init` first"
        ) from exc


def _resolve_profile(project_root: Path, m: Manifest, layout_profile: str | None) -> layout_profiles.LayoutProfile:
    active_name = layout_profile or m.layout_profile
    if active_name:
        try:
            return layout_profiles.get_profile(project_root, active_name)
        except layout_profiles.LayoutProfileNotFoundError:
            return layout_profiles.BUILTIN_CLAUDE
    return layout_profiles.BUILTIN_CLAUDE


def _locked_repo_pairs(m: Manifest) -> dict[str, str]:
    """Map repo_alias -> repo_url for every skill and agent in the lock."""
    pairs: dict[str, str] = {}
    for s in m.skills:
        pairs[s.repo_alias] = s.repo_url
    for a in m.agents:
        pairs[a.repo_alias] = a.repo_url
    return pairs


def _register_repo(alias: str, url: str, allow_insecure: bool) -> str | None:
    """Register a repo and index its artifacts. Return an error string or None."""
    try:
        repos.get(alias)
        # Already registered; just make sure indexes are current.
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


async def _ensure_repos(pairs: dict[str, str], allow_insecure: bool) -> list[str]:
    """Concurrently register missing repos. Return list of error strings."""
    if not pairs:
        return []

    async def _one(alias: str, url: str) -> str | None:
        return await asyncio.to_thread(_register_repo, alias, url, allow_insecure)

    results = await asyncio.gather(*(_one(alias, url) for alias, url in pairs.items()))
    return [r for r in results if r is not None]


def _resolve_target_dir(project_root: Path, target_dir: str) -> Path:
    safe = paths.safe_project_path(project_root, target_dir)
    if safe is None:
        raise SyncError(f"target_dir escapes project root: {target_dir!r}")
    return safe


def _resolve_agent_target(project_root: Path, target_path: str) -> Path:
    safe = paths.safe_project_path(project_root, target_path)
    if safe is None:
        raise SyncError(f"target_path escapes project root: {target_path!r}")
    return safe


def _sync_skill(
    project_root: Path,
    installed: InstalledSkill,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single skill. Returns (synced_qn or None, error or None)."""
    try:
        target = _resolve_target_dir(project_root, installed.target_dir)
    except SyncError as exc:
        return None, str(exc)

    if target.exists() and installed.content_hash is not None:
        current = hashing.hash_tree(target)
        if current == installed.content_hash:
            return None, None
        if not force:
            raise SyncDriftError(
                f"{installed.qualified_name}: {installed.target_dir} edited since install; "
                "pass --force to overwrite"
            )

    # Build an install plan from the locked sha and deploy exact snapshot bytes.
    try:
        plan = install_mod.InstallPlan(
            qualified_name=installed.qualified_name,
            repo_alias=installed.repo_alias,
            skill_name=installed.qualified_name.split("/", 1)[1],
            source_path=installed.source_path,
            target_dir=target,
            version=installed.current,
        )
        content_hash = install_mod._deploy(plan)
    except Exception as exc:
        return None, f"{installed.qualified_name}: {exc}"

    installed.content_hash = content_hash
    return installed.qualified_name, None


async def _sync_skills(
    project_root: Path,
    skills: list[InstalledSkill],
    *,
    force: bool,
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[str], list[str]]:
    """Reconcile all skills concurrently. Returns (synced, errors)."""
    if not skills:
        return [], []

    async def _one(skill: InstalledSkill, cb: Callable[[str, str, str], object] | None) -> tuple[str | None, str | None]:
        _notify(cb, "skill", skill.qualified_name, "syncing")
        try:
            synced, error = await asyncio.to_thread(_sync_skill, project_root, skill, force=force)
        except SyncDriftError as exc:
            return None, str(exc)
        if error:
            _notify(cb, "skill", skill.qualified_name, "error")
        else:
            _notify(cb, "skill", skill.qualified_name, "ok")
        return synced, error

    results = await asyncio.gather(*(_one(s, callback) for s in skills))
    synced = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return synced, errors


def _read_agent_at_sha(installed: InstalledAgent) -> str:
    repo_dir = repos.clone_dir(installed.repo_alias)
    if installed.source_path.endswith(".md"):
        artifact_path = installed.source_path
    else:
        artifact_path = f"{installed.source_path}/AGENT.md"
    return git.get_backend().cat_file(repo_dir, installed.current.sha, artifact_path)


def _sync_agent(
    project_root: Path,
    installed: InstalledAgent,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single agent. Returns (synced_qn or None, error or None)."""
    try:
        target = _resolve_agent_target(project_root, installed.target_path)
    except SyncError as exc:
        return None, str(exc)

    try:
        expected_content = _read_agent_at_sha(installed)
    except Exception as exc:
        return None, f"{installed.qualified_name}: could not read source at {installed.current.sha[:12]}: {exc}"

    expected_hash = hashing.hash_text(expected_content)

    if target.exists() and installed.content_hash is not None:
        current_hash = hashing.hash_text(target.read_text(encoding="utf-8"))
        if current_hash == installed.content_hash:
            return None, None
        if not force:
            raise SyncDriftError(
                f"{installed.qualified_name}: {installed.target_path} edited since install; "
                "pass --force to overwrite"
            )

    try:
        content_guard.assert_no_hidden_unicode(
            expected_content, source=f"agent {installed.qualified_name}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected_content, encoding="utf-8")
    except Exception as exc:
        return None, f"{installed.qualified_name}: failed to write {target}: {exc}"

    installed.content_hash = expected_hash
    return installed.qualified_name, None


async def _sync_agents(
    project_root: Path,
    agents: list[InstalledAgent],
    *,
    force: bool,
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[str], list[str]]:
    if not agents:
        return [], []

    async def _one(agent: InstalledAgent, cb: Callable[[str, str, str], object] | None) -> tuple[str | None, str | None]:
        _notify(cb, "agent", agent.qualified_name, "syncing")
        try:
            synced, error = await asyncio.to_thread(_sync_agent, project_root, agent, force=force)
        except SyncDriftError as exc:
            return None, str(exc)
        if error:
            _notify(cb, "agent", agent.qualified_name, "error")
        else:
            _notify(cb, "agent", agent.qualified_name, "ok")
        return synced, error

    results = await asyncio.gather(*(_one(a, callback) for a in agents))
    synced = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return synced, errors


def _sync_mcp(
    project_root: Path,
    installed: InstalledMcpServer,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single MCP entry. Returns (synced_alias or None, error or None)."""
    try:
        mcp_install._check_local_edits(project_root, installed, force=force)
    except mcp_install.McpLocalEditsError as exc:
        return None, str(exc)

    try:
        mcp_registry.merge_mcp_server(project_root, installed.alias, installed.entry)
    except Exception as exc:
        return None, f"{installed.alias}: failed to restore MCP entry: {exc}"

    return installed.alias, None


async def _sync_mcps(
    project_root: Path,
    mcps: list[InstalledMcpServer],
    *,
    force: bool,
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[str], list[str]]:
    if not mcps:
        return [], []

    async def _one(mcp: InstalledMcpServer, cb: Callable[[str, str, str], object] | None) -> tuple[str | None, str | None]:
        _notify(cb, "mcp", mcp.alias, "syncing")
        synced, error = await asyncio.to_thread(_sync_mcp, project_root, mcp, force=force)
        if error:
            _notify(cb, "mcp", mcp.alias, "error")
        else:
            _notify(cb, "mcp", mcp.alias, "ok")
        return synced, error

    results = await asyncio.gather(*(_one(m, callback) for m in mcps))
    synced = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return synced, errors


def _render_agent_files(
    project_root: Path,
    m: Manifest,
    profile: layout_profiles.LayoutProfile,
    *,
    force: bool,
) -> list[str]:
    """Re-render AGENTS.md and recreate symlinks. Returns drift warnings."""
    return agent_files.write_agent_files(
        project_root, m, profile, force=force
    )


async def run(options: SyncOptions) -> SyncResult:
    project_root = options.project_root.resolve()
    m = _load_lock(project_root)
    profile = _resolve_profile(project_root, m, options.layout_profile)

    result = SyncResult(project_root=project_root)

    # 1. Ensure repos.
    _notify(options.progress_callback, "repos", "all", "syncing")
    repo_pairs = _locked_repo_pairs(m)
    result.repo_errors = await _ensure_repos(repo_pairs, options.allow_insecure)
    _notify(options.progress_callback, "repos", "all", "ok")

    # 2. Apply rules on the main thread (DB + small files).
    _notify(options.progress_callback, "rules", "all", "syncing")
    project_rules_dir = project_root / profile.rules_dir
    applied = rules_mod.apply_to_project(
        project_root, list(m.rules), rules_mode=profile.rules_mode, rules_dir=project_rules_dir
    )
    result.rules_applied = [r.name for r in applied]
    _notify(options.progress_callback, "rules", "all", "ok")

    # 3. Reconcile skills.
    skills_synced, skill_errors = await _sync_skills(
        project_root, m.skills, force=options.force, callback=options.progress_callback
    )
    result.synced_skills = skills_synced

    # 4. Reconcile agents.
    agents_synced, agent_errors = await _sync_agents(
        project_root, m.agents, force=options.force, callback=options.progress_callback
    )
    result.synced_agents = agents_synced

    # 5. Reconcile MCP servers.
    mcps_synced, mcp_errors = await _sync_mcps(
        project_root, m.mcp_servers, force=options.force, callback=options.progress_callback
    )
    result.synced_mcp = mcps_synced

    # 6. Re-render AGENTS.md and symlinks when requested.
    if options.sync_agents:
        result.drift_warnings = await asyncio.to_thread(
            _render_agent_files, project_root, m, profile, force=options.force
        )

    # 7. Persist the lockfile (content_hash fields may have been refreshed).
    # Keep this on the main thread to avoid concurrent manifest writes.
    manifest.save(project_root, m)

    # Surface the first blocking error so callers get a clear failure.
    all_errors = result.repo_errors + skill_errors + agent_errors + mcp_errors
    if all_errors:
        # We already saved the partial state so re-run picks up progress.
        raise SyncError("; ".join(all_errors))

    return result
