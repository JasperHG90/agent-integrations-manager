"""Sub-agent install / update / delete / rollback.

Mirrors the skill lifecycle but writes a single Markdown file under the active
layout profile's `agents_dir` (e.g. `.claude/agents/<name>.md`).
"""

from __future__ import annotations

from pathlib import Path

from agent_init.core import agents, hashing, layout_profiles, manifest, paths, validation
from agent_init.core.install import resolve_install_version
from agent_init.core.models import InstalledAgent, Manifest, SkillVersion


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
    pass


class AgentNameMismatchWarning:
    """Frontmatter `name` differs from the directory-derived target name."""

    def __init__(self, expected: str, found: str | None):
        self.expected = expected
        self.found = found


_agent_install_warnings: list[str] = []


def take_install_warnings() -> list[str]:
    """Drain the agent install-warning buffer. CLI/TUI surfaces these."""
    out = list(_agent_install_warnings)
    _agent_install_warnings.clear()
    return out


def _load_manifest(project_root: Path) -> Manifest:
    return manifest.load_or_default(project_root)


def _find_installed(m: Manifest, qualified_name: str) -> InstalledAgent | None:
    for a in m.agents:
        if a.qualified_name == qualified_name:
            return a
    return None


def _agent_index_row(qualified_name: str) -> agents.AgentIndex:
    from agent_init.core.models import AgentIndex

    with agents.db.session() as session:
        row = session.get(AgentIndex, qualified_name)
    if row is None:
        raise AgentNotIndexedError(qualified_name)
    return row


def _target_path(project_root: Path, agent_name: str) -> Path:
    if not validation.is_valid_agent_name(agent_name):
        raise ValueError(f"agent name {agent_name!r} is not a safe file name")
    profile = layout_profiles.resolve_active(project_root)
    rel = f"{profile.agents_dir}/{agent_name}.md"
    safe = paths.safe_project_path(project_root, rel)
    if safe is None:
        raise ValueError(f"agent target path escapes the project: {rel}")
    return safe


def _check_local_edits(project_root: Path, installed: InstalledAgent, *, force: bool) -> None:
    if force or installed.content_hash is None:
        return
    target = paths.safe_project_path(project_root, installed.target_path)
    if target is None or not target.exists():
        return
    current = hashing.hash_text(target.read_text(encoding="utf-8"))
    if current != installed.content_hash:
        raise AgentLocalEditsError(
            f"{installed.qualified_name}: {target} has been modified since install. "
            "Pass force=True (`--force`) to overwrite."
        )


def install(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
) -> InstalledAgent:
    """Install a sub-agent into the project's agents directory."""
    row = _agent_index_row(qualified_name)
    version = resolve_install_version(
        row.repo_alias, row.source_path, track=track, pin=pin, artifact_name="AGENT.md"
    )
    content = agents.read_agent_content(qualified_name)

    frontmatter, _ = agents._extract_frontmatter(content)
    fm_name = frontmatter.get("name")
    if isinstance(fm_name, str) and fm_name != row.agent_name:
        _agent_install_warnings.append(
            f"{qualified_name}: frontmatter name {fm_name!r} differs from "
            f"directory name {row.agent_name!r}"
        )

    target = _target_path(project_root, row.agent_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    content_hash = hashing.hash_text(content)

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
        )
        m.agents.append(installed)
        result = installed
    else:
        existing.push_history(version)
        existing.repo_alias = row.repo_alias
        existing.source_path = row.source_path
        existing.target_path = str(target.relative_to(project_root))
        existing.content_hash = content_hash
        if pin is not None:
            existing.pin = pin
        if track is not None:
            existing.track = track
        result = existing
    manifest.save(project_root, m)
    return result


def update(
    project_root: Path,
    qualified_name: str,
    *,
    force: bool = False,
) -> InstalledAgent:
    """Refresh an installed sub-agent from its source repo."""
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
    target = project_root / existing.target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    existing.push_history(new_version)
    existing.content_hash = hashing.hash_text(content)
    manifest.save(project_root, m)
    return existing


def update_many(
    project_root: Path,
    *,
    repo_alias: str | None = None,
    only_outdated: bool = False,
    force: bool = False,
) -> list[dict]:
    """Update all (or a filtered subset of) installed agents in a project."""
    from dataclasses import dataclass

    @dataclass
    class Outcome:
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
            result = update(project_root, agent.qualified_name, force=force)
            outcomes.append(
                Outcome(agent.qualified_name, "updated", result.current.identifier())
            )
        except Exception as exc:
            outcomes.append(Outcome(agent.qualified_name, "error", str(exc)))
    return [{"qualified_name": o.qualified_name, "status": o.status, "detail": o.detail} for o in outcomes]


def delete(project_root: Path, qualified_name: str) -> None:
    """Remove an installed sub-agent file and its manifest entry."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise AgentNotInstalledError(qualified_name)
    target = project_root / existing.target_path
    if target.exists():
        target.unlink()
    m.agents = [a for a in m.agents if a.qualified_name != qualified_name]
    manifest.save(project_root, m)


def rollback(project_root: Path, qualified_name: str, *, force: bool = False) -> InstalledAgent:
    """Restore `history[0]` as the current installed sub-agent."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise AgentNotInstalledError(qualified_name)
    if not existing.history:
        raise AgentNoHistoryToRollbackError(qualified_name)
    _check_local_edits(project_root, existing, force=force)
    target_version = existing.history[0]

    repo_dir = agents.repos.clone_dir(existing.repo_alias)
    content = agents.git.get_backend().cat_file(
        repo_dir, target_version.sha, f"{existing.source_path}/AGENT.md"
    )

    target = project_root / existing.target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    existing.push_history(
        SkillVersion(
            tag=target_version.tag,
            sha=target_version.sha,
            installed_at=target_version.installed_at,
        )
    )
    existing.content_hash = hashing.hash_text(content)
    manifest.save(project_root, m)
    return existing
