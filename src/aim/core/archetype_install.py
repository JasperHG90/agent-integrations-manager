"""Select, update, and clear a project's instruction archetype (a singleton).

`select` records the chosen archetype in aim.toml + aim.lock.toml (SHA-pinned and
content-hashed) and renders AGENTS.md from the archetype body with aim's managed
`rules` region merged in. `clear` reverts to the built-in instruction template.
All archetype content passes the same policy/security/risk gate as other artifacts.
"""

from __future__ import annotations

from pathlib import Path

from aim.core import (
    agent_files,
    archetypes,
    content_guard,
    declarations,
    hashing,
    layout_profiles,
    manifest,
    policy,
    repos,
    risk,
)
from aim.core.install import resolve_install_version
from aim.core.models import InstalledArchetype


class NoArchetypeSelectedError(ValueError):
    """Raised when an update is requested but no archetype is selected."""


def _repo_url(alias: str) -> str:
    """Return the URL for a registered repo alias, or empty string if unknown."""
    try:
        return repos.get(alias).url
    except repos.RepoNotFoundError:
        return ""


def _gate_archetype(
    project_root: Path, qualified_name: str, content: str, *, override_risk: bool = False
) -> None:
    """Run repo/policy/security/risk checks on an archetype's base body.

    Args:
        project_root: Root of the project whose effective policy applies.
        qualified_name: The repo-qualified archetype name being gated.
        content: The archetype base instruction body to inspect.
        override_risk: Bypass the risk gate when True.
    """
    pol = policy.effective_policy(project_root)
    alias = qualified_name.split("/", 1)[0]
    policy.assert_repo_allowed(pol, alias, _repo_url(alias))
    policy.assert_archetype_allowed(pol, qualified_name)
    content_guard.assert_no_hidden_unicode(content, source=f"archetype {qualified_name}")
    risk.gate(content, qualified_name=qualified_name, pol=pol, override_risk=override_risk)


def _render(project_root: Path, m: object) -> None:
    """Re-render AGENTS.md from the manifest and persist the manifest."""
    profile = layout_profiles.resolve_active(project_root)
    agent_files.write_agent_files(project_root, m, profile, force=True)
    manifest.save(project_root, m)  # type: ignore[arg-type]


def select(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
    override_risk: bool = False,
) -> InstalledArchetype:
    """Select an instruction archetype as the project's AGENTS.md base.

    Records the selection in aim.toml; if the project is already locked, pins it in
    the manifest and re-renders AGENTS.md immediately. Otherwise the first `aim lock`
    resolves and renders it.

    Args:
        project_root: Root of the project to select into.
        qualified_name: The repo-qualified archetype name to select.
        track: Optional update track to follow instead of pinning.
        pin: Optional explicit version pin.
        override_risk: Bypass the risk gate when True.

    Returns:
        The locked archetype entry (new or updated in place).
    """
    row = archetypes.index_row(qualified_name)
    version = resolve_install_version(
        row.repo_alias,
        row.instruction_path,
        track=track,
        pin=pin,
        artifact_name=Path(row.instruction_path).name,
    )
    content = archetypes.read_base_body(row.repo_alias, version.sha, row.instruction_path)
    _gate_archetype(project_root, qualified_name, content, override_risk=override_risk)
    installed = InstalledArchetype(
        qualified_name=qualified_name,
        repo_alias=row.repo_alias,
        repo_url=repos.get(row.repo_alias).url,
        source_path=row.instruction_path,
        current=version,
        content_hash=hashing.hash_text(content),
        pin=pin,
        track=track,
        risk_acknowledged=override_risk,
    )
    declarations.set_archetype(project_root, installed)

    try:
        m = manifest.load(project_root)
    except manifest.ManifestNotFoundError:
        return installed  # declaration recorded; the first `aim lock` will render it.

    previous = m.archetype
    if previous is not None and previous.qualified_name == qualified_name:
        previous.push_history(version)
        previous.repo_alias = row.repo_alias
        previous.repo_url = installed.repo_url
        previous.source_path = row.instruction_path
        previous.content_hash = installed.content_hash
        if override_risk:
            previous.risk_acknowledged = True
        if pin is not None:
            previous.pin = pin
        if track is not None:
            previous.track = track
        installed = previous
    m.archetype = installed
    _render(project_root, m)
    return installed


def update(project_root: Path, *, override_risk: bool = False) -> InstalledArchetype:
    """Re-resolve the selected archetype to its tracked ref and re-render.

    Args:
        project_root: Root of the project to update.
        override_risk: Bypass the risk gate when True.

    Returns:
        The updated archetype entry.

    Raises:
        NoArchetypeSelectedError: If no archetype is selected.
    """
    declared = declarations.load_or_default(project_root).archetype
    if declared.is_builtin:
        raise NoArchetypeSelectedError("no instruction archetype is selected")
    return select(
        project_root,
        declared.qualified_name,
        track=declared.track,
        pin=declared.pin,
        override_risk=override_risk,
    )


def clear(project_root: Path) -> None:
    """Clear the selected archetype, reverting AGENTS.md to the built-in template."""
    declarations.clear_archetype(project_root)
    try:
        m = manifest.load(project_root)
    except manifest.ManifestNotFoundError:
        return
    m.archetype = None
    _render(project_root, m)
