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
    agent_install,
    agents,
    archetypes,
    git,
    hashing,
    layout_profiles,
    manifest,
    mcp_install,
    mcp_registry,
    paths,
    plugin_install,
    plugin_kinds,
    plugins,
    policy,
    repos,
    rule_install,
    skills,
    target_install,
    targets,
)
from aim.core import (
    install as install_mod,
)
from aim.core import (
    repo_rules as repo_rules_mod,
)
from aim.core.models import (
    InstalledAgent,
    InstalledMcpServer,
    InstalledPlugin,
    InstalledRule,
    InstalledSkill,
    InstalledTarget,
    Manifest,
)


class SyncError(RuntimeError):
    """Top-level sync failure (missing lockfile, unreachable repo, etc.).

    Carries the individual per-artifact failures so the CLI can list them one per
    line instead of one long semicolon-joined string.
    """

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        """Store the joined message and the underlying per-artifact errors.

        Args:
            message: The combined, human-readable failure message.
            errors: The individual failure strings, when the failure aggregates many.
        """
        super().__init__(message)
        self.errors = errors or []


class SyncDriftError(RuntimeError):
    """A managed artifact was edited locally and `force=False`."""


class SyncRepoError(RuntimeError):
    """A source repo could not be registered or indexed."""


@dataclass
class SyncOptions:
    """Configuration for a single `aim sync` run."""

    project_root: Path
    force: bool = False
    sync_agents: bool = True
    layout_profile: str | None = None
    progress_callback: Callable[[str, str, str], object] | None = None
    allow_insecure: bool = False


@dataclass
class SyncResult:
    """Outcome of a sync run: what was reconciled and any warnings or errors."""

    project_root: Path
    synced_skills: list[str] = field(default_factory=list)
    synced_agents: list[str] = field(default_factory=list)
    synced_mcp: list[str] = field(default_factory=list)
    synced_plugins: list[str] = field(default_factory=list)
    synced_targets: list[str] = field(default_factory=list)
    drift_warnings: list[str] = field(default_factory=list)
    repo_errors: list[str] = field(default_factory=list)
    rules_applied: list[str] = field(default_factory=list)


def _notify(
    callback: Callable[[str, str, str], object] | None, kind: str, name: str, status: str
) -> None:
    """Invoke the optional progress callback, swallowing any error it raises.

    Args:
        callback: User-supplied progress hook, or None to do nothing.
        kind: Artifact category (e.g. "skill", "agent", "rule", "mcp").
        name: Identifier of the artifact being reported on.
        status: Lifecycle status such as "syncing", "ok", or "error".
    """
    if callback is not None:
        try:
            callback(kind, name, status)
        except Exception:
            pass


def _load_lock(project_root: Path) -> Manifest:
    """Load the lockfile for a project, raising SyncError if it is missing.

    Args:
        project_root: Directory expected to contain `aim.lock.toml`.

    Returns:
        The parsed manifest.

    Raises:
        SyncError: No lockfile was found in `project_root`.
    """
    try:
        return manifest.load(project_root)
    except manifest.ManifestNotFoundError as exc:
        raise SyncError(f"no aim.lock.toml in {project_root}; run `aim init` first") from exc


def _resolve_profile(
    project_root: Path, m: Manifest, layout_profile: str | None
) -> layout_profiles.LayoutProfile:
    """Resolve the active layout profile, falling back to the built-in Claude one.

    Args:
        project_root: Project directory used to look up custom profiles.
        m: Loaded manifest providing the lockfile's default profile name.
        layout_profile: Explicit profile name that overrides the manifest's.

    Returns:
        The resolved profile, or the built-in Claude profile if none matches.
    """
    active_name = layout_profile or m.layout_profile
    if active_name:
        try:
            return layout_profiles.get_profile(project_root, active_name)
        except layout_profiles.LayoutProfileNotFoundError:
            return layout_profiles.BUILTIN_CLAUDE
    return layout_profiles.BUILTIN_CLAUDE


def _locked_repo_pairs(m: Manifest) -> dict[str, str]:
    """Collect the repo_alias to repo_url mapping for all locked artifacts.

    Args:
        m: Loaded manifest whose skills, agents, and rules are scanned.

    Returns:
        A mapping of repo alias to repo URL for every locked artifact.
    """
    pairs: dict[str, str] = {}
    for s in m.skills:
        pairs[s.repo_alias] = s.repo_url
    for a in m.agents:
        pairs[a.repo_alias] = a.repo_url
    for r in m.rules:
        pairs[r.repo_alias] = r.repo_url
    for p in m.plugins:
        pairs[p.repo_alias] = p.repo_url
    for t in m.targets:
        pairs[t.repo_alias] = t.repo_url
    if m.archetype is not None:
        pairs[m.archetype.repo_alias] = m.archetype.repo_url
    return pairs


def _register_repo(alias: str, url: str, allow_insecure: bool) -> str | None:
    """Register a source repo if needed and index its artifacts.

    Args:
        alias: Local alias to register the repo under.
        url: Remote URL to clone when the repo is not yet registered.
        allow_insecure: Permit insecure (e.g. non-HTTPS) transports.

    Returns:
        An error string describing a registration failure, or None on success.
    """
    # Identity-level dedup: the URL may already be registered under a different
    # local alias (a teammate's lockfile loads to whichever alias is local). Reuse
    # that registration rather than cloning a duplicate.
    existing = repos.get_by_url(url)
    if existing is not None:
        alias = existing.alias
    try:
        repo = repos.get(alias)
        # Re-indexing (DELETE + many INSERT per kind, serialized through the DB lock)
        # is the bulk of sync's DB work. It only refreshes the search index, which is
        # already rebuilt on add/refresh at `last_sha` — so skip it when the clone is
        # unchanged. The sha read is a cheap local git call (no fetch happens here).
        current_sha = git.get_backend().resolve_ref(repos.clone_dir(alias), repo.default_ref)
        if current_sha != repo.last_sha:
            skills.index_repo(alias)
            agents.index_repo(alias)
            repo_rules_mod.index_repo(alias)
            archetypes.index_repo(alias)
            plugins.index_repo(alias)
            targets.index_repo(alias)
        return None
    except repos.RepoNotFoundError:
        pass
    # Not registered anywhere: auto-add under a default owner-repo alias (the alias
    # from the lockfile is only a per-machine label and may collide locally).
    add_alias = alias if not _alias_taken(alias) else repos.derive_default_alias(url)
    try:
        repos.add(add_alias, url, allow_empty=True, allow_insecure=allow_insecure)
    except Exception as exc:
        return f"repo {add_alias}: failed to register {url}: {exc}"
    return None


def _alias_taken(alias: str) -> bool:
    """Return whether a repo alias is already registered locally."""
    try:
        repos.get(alias)
    except repos.RepoNotFoundError:
        return False
    return True


async def _ensure_repos(pairs: dict[str, str], allow_insecure: bool) -> list[str]:
    """Concurrently register and index every repo in the alias-to-url mapping.

    Args:
        pairs: Mapping of repo alias to remote URL to ensure.
        allow_insecure: Permit insecure transports when cloning.

    Returns:
        A list of error strings, one per repo that failed to register.
    """
    if not pairs:
        return []

    async def _one(alias: str, url: str) -> str | None:
        """Register one repo off the event loop and return its error or None."""
        return await asyncio.to_thread(_register_repo, alias, url, allow_insecure)

    results = await asyncio.gather(*(_one(alias, url) for alias, url in pairs.items()))
    return [r for r in results if r is not None]


def _resolve_target_dir(project_root: Path, target_dir: str) -> Path:
    """Resolve a skill target directory, rejecting paths that escape the root.

    Args:
        project_root: Project root that the target must stay within.
        target_dir: Relative target directory from the lockfile.

    Returns:
        The resolved absolute target directory.

    Raises:
        SyncError: The target directory escapes `project_root`.
    """
    safe = paths.safe_project_path(project_root, target_dir)
    if safe is None:
        raise SyncError(f"target_dir escapes project root: {target_dir!r}")
    return safe


def _resolve_agent_target(project_root: Path, target_path: str) -> Path:
    """Resolve an agent target path, rejecting paths that escape the root.

    Args:
        project_root: Project root that the target must stay within.
        target_path: Relative target file path from the lockfile.

    Returns:
        The resolved absolute target path.

    Raises:
        SyncError: The target path escapes `project_root`.
    """
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
    """Reconcile a single skill against its locked snapshot.

    Args:
        installed: Locked skill record describing the target and version.
        force: Overwrite local edits instead of raising on drift.

    Returns:
        A tuple of (synced qualified name or None, error string or None). The
        qualified name is None when the skill was already up to date.

    Raises:
        SyncDriftError: The target was edited since install and `force` is False.
    """
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
            project_root=project_root,
        )
        content_hash = install_mod._deploy(plan, override_risk=installed.risk_acknowledged)
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
    """Reconcile all locked skills concurrently.

    Args:
        skills: Locked skill records to reconcile.
        force: Overwrite local edits instead of treating drift as an error.
        callback: Optional progress hook invoked per skill.

    Returns:
        A tuple of (synced qualified names, error strings).
    """
    if not skills:
        return [], []

    async def _one(
        skill: InstalledSkill, cb: Callable[[str, str, str], object] | None
    ) -> tuple[str | None, str | None]:
        """Reconcile one skill off-thread and emit its progress notifications."""
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
    """Read an agent's source content at its locked commit.

    Args:
        installed: Locked agent record providing the repo, sha, and source path.

    Returns:
        The agent instruction file contents at the locked sha.
    """
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
    """Reconcile a single agent instruction file against its locked content.

    Args:
        installed: Locked agent record describing the target and version.
        force: Overwrite local edits instead of raising on drift.

    Returns:
        A tuple of (synced qualified name or None, error string or None). The
        qualified name is None when the agent was already up to date.

    Raises:
        SyncDriftError: The target was edited since install and `force` is False.
    """
    try:
        target = _resolve_agent_target(project_root, installed.target_path)
    except SyncError as exc:
        return None, str(exc)

    try:
        expected_content = _read_agent_at_sha(installed)
    except Exception as exc:
        return (
            None,
            f"{installed.qualified_name}: could not read source at {installed.current.sha[:12]}: {exc}",
        )

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
        agent_install._gate_agent(
            project_root,
            installed.qualified_name,
            expected_content,
            override_risk=installed.risk_acknowledged,
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
    """Reconcile all locked agents concurrently.

    Args:
        agents: Locked agent records to reconcile.
        force: Overwrite local edits instead of treating drift as an error.
        callback: Optional progress hook invoked per agent.

    Returns:
        A tuple of (synced qualified names, error strings).
    """
    if not agents:
        return [], []

    async def _one(
        agent: InstalledAgent, cb: Callable[[str, str, str], object] | None
    ) -> tuple[str | None, str | None]:
        """Reconcile one agent off-thread and emit its progress notifications."""
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


def _read_rule_at_sha(installed: InstalledRule) -> str:
    """Read a rule's source content at its locked commit.

    Args:
        installed: Locked rule record providing the repo, sha, and source path.

    Returns:
        The rule file contents at the locked sha.
    """
    repo_dir = repos.clone_dir(installed.repo_alias)
    return git.get_backend().cat_file(repo_dir, installed.current.sha, installed.source_path)


def _sync_rule(
    project_root: Path,
    installed: InstalledRule,
    profile: layout_profiles.LayoutProfile,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single rule against its locked content.

    In inline mode the body is composed into AGENTS.md by the render step, so
    there is nothing to deploy here. In files mode the body is written to
    `<rules_dir>/<name>.md` with a drift guard.

    Args:
        profile: Active layout profile deciding files vs inline rule mode.
        force: Overwrite local edits instead of raising on drift.

    Returns:
        A tuple of (synced qualified name or None, error string or None).

    Raises:
        SyncDriftError: The target was edited since install and `force` is False.
    """
    if profile.rules_mode != "files":
        return installed.qualified_name, None

    rule_name = installed.qualified_name.split("/", 1)[-1]
    rel = f"{profile.rules_dir}/{rule_name}.md"
    target = paths.safe_project_path(project_root, rel)
    if target is None:
        return None, f"{installed.qualified_name}: target path escapes project: {rel}"

    try:
        expected_content = _read_rule_at_sha(installed)
    except Exception as exc:
        return (
            None,
            f"{installed.qualified_name}: could not read source at {installed.current.sha[:12]}: {exc}",
        )

    expected_hash = hashing.hash_text(expected_content)

    if target.exists() and installed.content_hash is not None:
        current_hash = hashing.hash_text(target.read_text(encoding="utf-8"))
        if current_hash == installed.content_hash:
            return installed.qualified_name, None
        if not force:
            raise SyncDriftError(
                f"{installed.qualified_name}: {rel} edited since install; pass --force to overwrite"
            )

    try:
        rule_install._gate_rule(
            project_root,
            installed.qualified_name,
            expected_content,
            override_risk=installed.risk_acknowledged,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected_content, encoding="utf-8")
    except Exception as exc:
        return None, f"{installed.qualified_name}: failed to write {target}: {exc}"

    installed.content_hash = expected_hash
    return installed.qualified_name, None


async def _sync_rules(
    project_root: Path,
    rules: list[InstalledRule],
    profile: layout_profiles.LayoutProfile,
    *,
    force: bool,
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[str], list[str]]:
    """Reconcile all locked rules concurrently.

    Args:
        rules: Locked rule records to reconcile.
        profile: Active layout profile deciding files vs inline rule mode.
        force: Overwrite local edits instead of treating drift as an error.
        callback: Optional progress hook invoked per rule.

    Returns:
        A tuple of (synced qualified names, error strings).
    """
    if not rules:
        return [], []

    async def _one(
        rule: InstalledRule, cb: Callable[[str, str, str], object] | None
    ) -> tuple[str | None, str | None]:
        """Reconcile one rule off-thread and emit its progress notifications."""
        _notify(cb, "rule", rule.qualified_name, "syncing")
        try:
            synced, error = await asyncio.to_thread(
                _sync_rule, project_root, rule, profile, force=force
            )
        except SyncDriftError as exc:
            return None, str(exc)
        if error:
            _notify(cb, "rule", rule.qualified_name, "error")
        else:
            _notify(cb, "rule", rule.qualified_name, "ok")
        return synced, error

    results = await asyncio.gather(*(_one(r, callback) for r in rules))
    synced = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return synced, errors


def _sync_target(
    project_root: Path,
    installed: InstalledTarget,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single target against its locked TOML, vendoring it to disk.

    Writes the locked content to ``.aim/targets/<name>.toml`` with a drift guard.

    Returns:
        A tuple of (synced qualified name or None, error string or None).

    Raises:
        SyncDriftError: The target was edited since install and `force` is False.
    """
    name = installed.qualified_name.split("/", 1)[-1]
    rel = f".aim/targets/{name}.toml"
    target = paths.safe_project_path(project_root, rel)
    if target is None:
        return None, f"{installed.qualified_name}: target path escapes project: {rel}"

    try:
        expected_content = git.get_backend().cat_file(
            repos.clone_dir(installed.repo_alias), installed.current.sha, installed.source_path
        )
    except Exception as exc:
        return (
            None,
            f"{installed.qualified_name}: could not read source at "
            f"{installed.current.sha[:12]}: {exc}",
        )

    expected_hash = hashing.hash_text(expected_content)

    if target.exists() and installed.content_hash is not None:
        current_hash = hashing.hash_text(target.read_text(encoding="utf-8"))
        if current_hash == installed.content_hash:
            return installed.qualified_name, None
        if not force:
            raise SyncDriftError(
                f"{installed.qualified_name}: {rel} edited since install; pass --force to overwrite"
            )

    try:
        target_install._gate_target(project_root, installed.qualified_name, expected_content)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected_content, encoding="utf-8")
    except Exception as exc:
        return None, f"{installed.qualified_name}: failed to write {target}: {exc}"

    installed.content_hash = expected_hash
    return installed.qualified_name, None


async def _sync_targets(
    project_root: Path,
    target_list: list[InstalledTarget],
    *,
    force: bool,
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[str], list[str]]:
    """Reconcile all locked targets concurrently."""
    if not target_list:
        return [], []

    async def _one(
        target: InstalledTarget, cb: Callable[[str, str, str], object] | None
    ) -> tuple[str | None, str | None]:
        """Reconcile one target off-thread and emit its progress notifications."""
        _notify(cb, "target", target.qualified_name, "syncing")
        try:
            synced, error = await asyncio.to_thread(_sync_target, project_root, target, force=force)
        except SyncDriftError as exc:
            return None, str(exc)
        if error:
            _notify(cb, "target", target.qualified_name, "error")
        else:
            _notify(cb, "target", target.qualified_name, "ok")
        return synced, error

    results = await asyncio.gather(*(_one(t, callback) for t in target_list))
    synced = [r[0] for r in results if r[0] is not None]
    errors = [r[1] for r in results if r[1] is not None]
    return synced, errors


def _sync_mcp(
    project_root: Path,
    installed: InstalledMcpServer,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single MCP server entry against its locked configuration.

    Args:
        installed: Locked MCP server record with alias, registry name, and entry.
        force: Overwrite local edits instead of failing on them.

    Returns:
        A tuple of (synced alias or None, error string or None).
    """
    try:
        policy.assert_mcp_allowed(
            policy.effective_policy(project_root), installed.alias, installed.registry_name
        )
    except policy.PolicyViolationError as exc:
        return None, str(exc)
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
    """Reconcile all locked MCP servers concurrently.

    Args:
        mcps: Locked MCP server records to reconcile.
        force: Overwrite local edits instead of treating them as an error.
        callback: Optional progress hook invoked per MCP server.

    Returns:
        A tuple of (synced aliases, error strings).
    """
    if not mcps:
        return [], []

    async def _one(
        mcp: InstalledMcpServer, cb: Callable[[str, str, str], object] | None
    ) -> tuple[str | None, str | None]:
        """Reconcile one MCP server off-thread and emit progress notifications."""
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


def _sync_plugin(
    project_root: Path,
    installed: InstalledPlugin,
    *,
    force: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a single vendored plugin against its locked snapshot.

    Re-vendors the plugin bytes from the locked SHA (the claude marketplace +
    settings registration is reconciled once afterward, in `run`). Returns
    (synced qualified name or None, error or None); the name is None when the
    vendored files already match.

    Raises:
        SyncDriftError: The vendored files were edited since install and not force.
    """
    target = paths.safe_project_path(project_root, installed.target_dir)
    if target is None:
        return None, f"{installed.qualified_name}: target escapes project: {installed.target_dir!r}"

    kind = plugin_kinds.get_kind(installed.flavor, project_root)
    if kind is None:
        return None, (
            f"{installed.qualified_name}: no plugin kind {installed.flavor!r} loaded; "
            "install its kind spec"
        )

    if target.exists() and installed.content_hash is not None:
        current = (
            hashing.hash_tree(target)
            if target.is_dir()
            else hashing.hash_text(target.read_text(encoding="utf-8"))
        )
        if current == installed.content_hash:
            return None, None
        if not force:
            raise SyncDriftError(
                f"{installed.qualified_name}: {installed.target_dir} edited since install; "
                "pass --force to overwrite"
            )

    try:
        plugin_name = installed.qualified_name.split("/", 1)[1]
        content_hash, _ = plugin_install._deploy(
            project_root,
            kind,
            repo_alias=installed.repo_alias,
            plugin_name=plugin_name,
            source_path=installed.source_path,
            version=installed.current,
            qualified_name=installed.qualified_name,
            override_risk=installed.risk_acknowledged,
        )
    except Exception as exc:
        return None, f"{installed.qualified_name}: {exc}"

    installed.content_hash = content_hash
    return installed.qualified_name, None


async def _sync_plugins(
    project_root: Path,
    plugins_list: list[InstalledPlugin],
    *,
    force: bool,
    callback: Callable[[str, str, str], object] | None,
) -> tuple[list[str], list[str]]:
    """Reconcile all locked plugins concurrently (vendoring only)."""
    if not plugins_list:
        return [], []

    async def _one(
        plugin: InstalledPlugin, cb: Callable[[str, str, str], object] | None
    ) -> tuple[str | None, str | None]:
        """Reconcile one plugin off-thread and emit its progress notifications."""
        _notify(cb, "plugin", plugin.qualified_name, "syncing")
        try:
            synced, error = await asyncio.to_thread(_sync_plugin, project_root, plugin, force=force)
        except SyncDriftError as exc:
            return None, str(exc)
        if error:
            _notify(cb, "plugin", plugin.qualified_name, "error")
        else:
            _notify(cb, "plugin", plugin.qualified_name, "ok")
        return synced, error

    results = await asyncio.gather(*(_one(p, callback) for p in plugins_list))
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
    """Re-render AGENTS.md and recreate symlinks for the project.

    Args:
        m: Loaded manifest supplying the artifacts to render.
        profile: Active layout profile controlling file placement.
        force: Overwrite locally edited generated files.

    Returns:
        A list of drift warnings for files that were skipped or overwritten.
    """
    return agent_files.write_agent_files(project_root, m, profile, force=force)


async def run(options: SyncOptions) -> SyncResult:
    """Reconcile the project to the state recorded in its lockfile.

    Registers source repos, then reconciles rules, skills, agents, and MCP
    servers, optionally re-renders agent files, and persists any refreshed
    content hashes back to the lockfile.

    Args:
        options: Sync configuration including the project root and flags.

    Returns:
        A SyncResult summarizing what was reconciled and any drift warnings.

    Raises:
        SyncError: The lockfile is missing or at least one artifact failed to
            reconcile (partial progress is saved before raising).
    """
    project_root = options.project_root.resolve()
    m = _load_lock(project_root)
    profile = _resolve_profile(project_root, m, options.layout_profile)

    result = SyncResult(project_root=project_root)

    _notify(options.progress_callback, "repos", "all", "syncing")
    repo_pairs = _locked_repo_pairs(m)
    result.repo_errors = await _ensure_repos(repo_pairs, options.allow_insecure)
    _notify(options.progress_callback, "repos", "all", "ok")

    # In files mode each rule is deployed and drift-guarded here; in inline mode
    # the rule bodies are instead rendered into AGENTS.md by the later step.
    rules_synced, rule_errors = await _sync_rules(
        project_root, m.rules, profile, force=options.force, callback=options.progress_callback
    )
    result.rules_applied = rules_synced

    skills_synced, skill_errors = await _sync_skills(
        project_root, m.skills, force=options.force, callback=options.progress_callback
    )
    result.synced_skills = skills_synced

    agents_synced, agent_errors = await _sync_agents(
        project_root, m.agents, force=options.force, callback=options.progress_callback
    )
    result.synced_agents = agents_synced

    mcps_synced, mcp_errors = await _sync_mcps(
        project_root, m.mcp_servers, force=options.force, callback=options.progress_callback
    )
    result.synced_mcp = mcps_synced

    # Targets must be vendored BEFORE plugins: plugin reconciliation resolves each
    # plugin's kind spec from `.aim/targets` on disk, so the target files have to
    # exist first.
    targets_synced, target_errors = await _sync_targets(
        project_root, m.targets, force=options.force, callback=options.progress_callback
    )
    result.synced_targets = targets_synced

    plugins_synced, plugin_errors = await _sync_plugins(
        project_root, m.plugins, force=options.force, callback=options.progress_callback
    )
    result.synced_plugins = plugins_synced
    # Reconcile each kind's client config (claude settings.json/marketplace, etc.)
    # once, after vendoring, to avoid races between concurrently-synced plugins.
    if m.plugins:
        await asyncio.to_thread(plugin_install.reconcile_registration, project_root, m)

    if options.sync_agents:
        result.drift_warnings = await asyncio.to_thread(
            _render_agent_files, project_root, m, profile, force=options.force
        )

    # Persist on the main thread (content_hash fields may have been refreshed)
    # to avoid concurrent manifest writes from the worker threads.
    manifest.save(project_root, m)

    all_errors = (
        result.repo_errors
        + rule_errors
        + skill_errors
        + agent_errors
        + mcp_errors
        + target_errors
        + plugin_errors
    )
    if all_errors:
        # Partial state was already saved above, so a re-run resumes progress.
        raise SyncError("; ".join(all_errors), errors=all_errors)

    return result
