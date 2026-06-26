"""Sub-agent install / update / delete / rollback.

Mirrors the skill lifecycle but writes a single Markdown file under the active
layout profile's `agents_dir` (e.g. `.claude/agents/<name>.md`).
"""

from __future__ import annotations

from pathlib import Path

from aim.core import (
    agents,
    content_guard,
    declarations,
    hashing,
    layout_profiles,
    manifest,
    paths,
    policy,
    risk,
    validation,
)
from aim.core.install import resolve_install_version
from aim.core.models import InstalledAgent, Manifest, SkillVersion


class AgentNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the agent index — try `repo refresh`."""


class AgentNotInstalledError(KeyError):
    """No entry for this agent in the project manifest."""


class AgentSourcePathChangedError(RuntimeError):
    """On `update`, the agent's source_path inside its repo differs from the
    installed version's. Aborts to avoid silently re-pointing the install."""


class AgentLocalEditsError(RuntimeError):
    """The deployed agent file has been edited by hand. Pass `force=True` to overwrite."""


class AgentNoHistoryToRollbackError(RuntimeError):
    """No prior version exists to roll the installed agent back to."""


class AgentManifestPathEscapeError(ValueError):
    """A manifest-stored target_path resolves outside the project root."""


class AgentNameMismatchWarning:
    """Frontmatter `name` differs from the directory-derived target name."""

    def __init__(self, expected: str, found: str | None):
        """Record the expected and actually-found agent names."""
        self.expected = expected
        self.found = found


_agent_install_warnings: list[str] = []


def take_install_warnings() -> list[str]:
    """Drain the agent install-warning buffer. CLI/TUI surfaces these."""
    out = list(_agent_install_warnings)
    _agent_install_warnings.clear()
    return out


def _load_manifest(project_root: Path) -> Manifest:
    """Load the project manifest, creating an empty one if absent."""
    return manifest.load_or_create(project_root)


def _find_installed(m: Manifest, qualified_name: str) -> InstalledAgent | None:
    """Return the installed agent matching the qualified name, or None.

    Args:
        m: The loaded project manifest.
        qualified_name: The repo-qualified agent name to look up.
    """
    for a in m.agents:
        if a.qualified_name == qualified_name:
            return a
    return None


def _agent_index_row(qualified_name: str) -> agents.AgentIndex:
    """Fetch the agent index row for a qualified name.

    Args:
        qualified_name: The repo-qualified agent name to look up.

    Raises:
        AgentNotIndexedError: The agent is absent from the index.
    """
    from aim.core.models import AgentIndex

    with agents.db.session() as session:
        row = session.get(AgentIndex, qualified_name)
    if row is None:
        raise AgentNotIndexedError(qualified_name)
    return row


def _target_path(project_root: Path, agent_name: str) -> Path:
    """Resolve the on-disk deploy path for an agent under the active layout.

    Args:
        project_root: Root of the target project.
        agent_name: Bare agent name (used as the Markdown file stem).

    Raises:
        ValueError: The name is unsafe or the resolved path escapes the project.
    """
    if not validation.is_valid_agent_name(agent_name):
        raise ValueError(f"agent name {agent_name!r} is not a safe file name")
    profile = layout_profiles.resolve_active(project_root)
    rel = f"{profile.agents_dir}/{agent_name}.md"
    safe = paths.safe_project_path(project_root, rel)
    if safe is None:
        raise ValueError(f"agent target path escapes the project: {rel}")
    return safe


def _resolve_target_path(project_root: Path, target_path: str) -> Path:
    """Validate a manifest-originated target_path and return its absolute path.

    Args:
        project_root: Root of the target project.
        target_path: Project-relative path recorded in the manifest.

    Raises:
        AgentManifestPathEscapeError: The path resolves outside the project root.
    """
    safe = paths.safe_project_path(project_root, target_path)
    if safe is None:
        raise AgentManifestPathEscapeError(
            f"manifest target_path escapes project root: {target_path!r}"
        )
    return safe


def _check_local_edits(project_root: Path, installed: InstalledAgent, *, force: bool) -> None:
    """Raise if the deployed agent file was hand-edited since install.

    Args:
        project_root: Root of the target project.
        installed: Manifest record holding the expected content hash.
        force: When True, skip the check and allow an overwrite.

    Raises:
        AgentLocalEditsError: The file's current hash differs from the recorded one.
    """
    if force or installed.content_hash is None:
        return
    target = _resolve_target_path(project_root, installed.target_path)
    if not target.exists():
        return
    current = hashing.hash_text(target.read_text(encoding="utf-8"))
    if current != installed.content_hash:
        raise AgentLocalEditsError(
            f"{installed.qualified_name}: {target} has been modified since install. "
            "Pass force=True (`--force`) to overwrite."
        )


def _repo_url(alias: str) -> str:
    """Return the URL for a repo alias, or empty string if unknown."""
    try:
        return agents.repos.get(alias).url
    except agents.repos.RepoNotFoundError:
        return ""


def _gate_agent(
    project_root: Path, qualified_name: str, content: str, *, override_risk: bool = False
) -> None:
    """Run the unified security/policy/risk gate for an agent's content.

    Every deploy path (install/update/rollback/sync) funnels through here, so the
    checks live in one place.

    Args:
        project_root: Root of the target project.
        qualified_name: Repo-qualified agent name (alias derives from it).
        content: Full agent Markdown to be vetted.
        override_risk: When True, downgrade risk-gate failures to allow the deploy.
    """
    pol = policy.effective_policy(project_root)
    alias = qualified_name.split("/", 1)[0]
    policy.assert_repo_allowed(pol, alias, _repo_url(alias))
    policy.assert_artifact_allowed(pol, "agent", qualified_name)
    content_guard.assert_no_hidden_unicode(content, source=f"agent {qualified_name}")
    risk.gate(
        content,
        qualified_name=qualified_name,
        pol=pol,
        override_risk=override_risk,
        kind="agent",
    )


def _write_agent(
    project_root: Path,
    qualified_name: str,
    content: str,
    target: Path,
    *,
    override_risk: bool = False,
) -> str:
    """Gate the content, write the agent file, and return its content hash.

    Args:
        project_root: Root of the target project.
        qualified_name: Repo-qualified agent name.
        content: Full agent Markdown to write.
        target: Absolute destination path for the file.
        override_risk: When True, bypass risk-gate failures.
    """
    _gate_agent(project_root, qualified_name, content, override_risk=override_risk)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return hashing.hash_text(content)


def install(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
    override_risk: bool = False,
) -> InstalledAgent:
    """Install a sub-agent into the project's agents directory.

    Args:
        project_root: Root of the target project.
        qualified_name: Repo-qualified agent name to install.
        track: Optional branch/tag track to resolve the version against.
        pin: Optional explicit version to pin the install to.
        override_risk: When True, bypass risk-gate failures.

    Returns:
        The created or updated manifest record for the installed agent.
    """
    row = _agent_index_row(qualified_name)
    version = resolve_install_version(
        row.repo_alias, row.source_path, track=track, pin=pin, artifact_name="AGENT.md"
    )
    content = agents.read_agent_content(qualified_name)

    # Warn (don't fail) when frontmatter name disagrees with the directory name;
    # the directory name remains authoritative for the deployed file.
    frontmatter, _ = agents._extract_frontmatter(content)
    fm_name = frontmatter.get("name")
    if isinstance(fm_name, str) and fm_name != row.agent_name:
        _agent_install_warnings.append(
            f"{qualified_name}: frontmatter name {fm_name!r} differs from "
            f"directory name {row.agent_name!r}"
        )

    target = _target_path(project_root, row.agent_name)
    content_hash = _write_agent(
        project_root, qualified_name, content, target, override_risk=override_risk
    )

    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        installed = InstalledAgent(
            qualified_name=qualified_name,
            repo_alias=row.repo_alias,
            repo_url=agents.repos.get(row.repo_alias).url,
            source_path=row.source_path,
            target_path=str(target.relative_to(project_root)),
            current=version,
            content_hash=content_hash,
            pin=pin,
            track=track,
            risk_acknowledged=override_risk,
        )
        m.agents.append(installed)
        result = installed
    else:
        existing.push_history(version)
        existing.repo_alias = row.repo_alias
        existing.source_path = row.source_path
        existing.target_path = str(target.relative_to(project_root))
        existing.content_hash = content_hash
        if override_risk:
            existing.risk_acknowledged = True
        if pin is not None:
            existing.pin = pin
        if track is not None:
            existing.track = track
        result = existing
    manifest.save(project_root, m)
    declarations._update_agent(project_root, result)
    return result


def update(
    project_root: Path,
    qualified_name: str,
    *,
    force: bool = False,
    override_risk: bool = False,
) -> InstalledAgent:
    """Refresh an installed sub-agent from its source repo.

    Args:
        project_root: Root of the target project.
        qualified_name: Repo-qualified agent name to update.
        force: When True, overwrite even if the file was edited locally.
        override_risk: When True, bypass risk-gate failures.

    Returns:
        The updated manifest record, unchanged if already at the resolved version.

    Raises:
        AgentNotInstalledError: The agent has no manifest entry.
        AgentSourcePathChangedError: Upstream source_path moved since install.
    """
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise AgentNotInstalledError(qualified_name)
    row = _agent_index_row(qualified_name)
    if row.source_path != existing.source_path:
        raise AgentSourcePathChangedError(
            f"{qualified_name}: source path moved from "
            f"{existing.source_path!r} (installed) to {row.source_path!r} (upstream). "
            "Reinstall explicitly to accept the move."
        )
    new_version = resolve_install_version(
        existing.repo_alias,
        existing.source_path,
        track=existing.track,
        pin=existing.pin,
        artifact_name="AGENT.md",
    )
    if new_version.sha == existing.current.sha:
        return existing

    _check_local_edits(project_root, existing, force=force)
    content = agents.read_agent_content(qualified_name)
    target = _resolve_target_path(project_root, existing.target_path)
    existing.push_history(new_version)
    existing.content_hash = _write_agent(
        project_root, qualified_name, content, target, override_risk=override_risk
    )
    if override_risk:
        existing.risk_acknowledged = True
    manifest.save(project_root, m)
    declarations._update_agent(project_root, existing)
    return existing


def update_many(
    project_root: Path,
    *,
    repo_alias: str | None = None,
    only_outdated: bool = False,
    force: bool = False,
    override_risk: bool = False,
) -> list[dict]:
    """Update all (or a filtered subset of) installed agents in a project.

    Args:
        project_root: Root of the target project.
        repo_alias: When set, only update agents from this repo alias.
        only_outdated: When True, skip agents already at their resolved HEAD.
        force: When True, overwrite agents edited locally.
        override_risk: When True, bypass a risk gate the user has acknowledged.

    Returns:
        One result dict per agent with keys ``qualified_name``, ``status``, ``detail``.
    """
    from dataclasses import dataclass

    @dataclass
    class Outcome:
        """Per-agent result of a bulk update pass."""

        qualified_name: str
        status: str
        detail: str = ""

    m = _load_manifest(project_root)
    outcomes: list[Outcome] = []
    for agent in list(m.agents):
        if repo_alias is not None and agent.repo_alias != repo_alias:
            outcomes.append(Outcome(agent.qualified_name, "skipped", "repo filter"))
            continue
        try:
            if only_outdated:
                _agent_index_row(agent.qualified_name)  # ensure still indexed
                new_version = resolve_install_version(
                    agent.repo_alias,
                    agent.source_path,
                    track=agent.track,
                    pin=agent.pin,
                    artifact_name="AGENT.md",
                )
                if new_version.sha == agent.current.sha:
                    outcomes.append(Outcome(agent.qualified_name, "noop", "at HEAD"))
                    continue
            result = update(
                project_root, agent.qualified_name, force=force, override_risk=override_risk
            )
            outcomes.append(Outcome(agent.qualified_name, "updated", result.current.identifier()))
        except Exception as exc:
            outcomes.append(Outcome(agent.qualified_name, "error", str(exc)))
    return [
        {"qualified_name": o.qualified_name, "status": o.status, "detail": o.detail}
        for o in outcomes
    ]


def delete(project_root: Path, qualified_name: str) -> None:
    """Remove an installed sub-agent file and its manifest entry.

    Args:
        project_root: Root of the target project.
        qualified_name: Repo-qualified agent name to remove.

    Raises:
        AgentNotInstalledError: The agent has no manifest entry.
    """
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise AgentNotInstalledError(qualified_name)
    target = _resolve_target_path(project_root, existing.target_path)
    if target.exists():
        target.unlink()
    m.agents = [a for a in m.agents if a.qualified_name != qualified_name]
    manifest.save(project_root, m)
    declarations._remove_agent(project_root, qualified_name)


def rollback(project_root: Path, qualified_name: str, *, force: bool = False) -> InstalledAgent:
    """Restore `history[0]` as the current installed sub-agent.

    Args:
        project_root: Root of the target project.
        qualified_name: Repo-qualified agent name to roll back.
        force: When True, overwrite even if the file was edited locally.

    Returns:
        The updated manifest record after the rollback.

    Raises:
        AgentNotInstalledError: The agent has no manifest entry.
        AgentNoHistoryToRollbackError: No prior version exists to restore.
    """
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise AgentNotInstalledError(qualified_name)
    if not existing.history:
        raise AgentNoHistoryToRollbackError(qualified_name)
    _check_local_edits(project_root, existing, force=force)
    target_version = existing.history[0]

    repo_dir = agents.repos.clone_dir(existing.repo_alias)
    if existing.source_path.endswith(".md"):
        artifact_path = existing.source_path
    else:
        artifact_path = f"{existing.source_path}/AGENT.md"
    content = agents.git.get_backend().cat_file(repo_dir, target_version.sha, artifact_path)

    target = _resolve_target_path(project_root, existing.target_path)
    existing.push_history(
        SkillVersion(
            tag=target_version.tag,
            sha=target_version.sha,
            installed_at=target_version.installed_at,
        )
    )
    existing.content_hash = _write_agent(project_root, qualified_name, content, target)
    manifest.save(project_root, m)
    declarations._update_agent(project_root, existing)
    return existing
