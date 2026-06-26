"""Skill install / update / delete / rollback.

Versioning follows the plan:

- Identifier: composite `<tag>+<short_sha>` when a tag is reachable and the
  skill's last-touching SHA is reachable from that tag (so the tag honestly
  describes the installed bytes); SHA-only otherwise.
- Install/update writes into `<project>/.claude/skills/<skill_name>/` by
  default. A snapshot of the same bytes is written into
  `user_cache_dir/snapshots/<repo_alias>/<sha>/<skill_name>/` so rollback
  survives upstream force-pushes / repo loss. Snapshots use a `.complete`
  sentinel so partial extractions are never mistaken for valid.
- `update` checks the deployed `target_dir` against the stored
  `content_hash`. If the user has edited installed files, update refuses
  unless `force=True`.
- Rollback restores `history[0]` of the manifest entry, preferring the
  local snapshot and falling back to `git archive` if absent.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import select

from aim.core import (
    content_guard,
    db,
    declarations,
    git,
    hashing,
    layout_profiles,
    manifest,
    paths,
    policy,
    repos,
    risk,
)
from aim.core.models import InstalledSkill, Manifest, SkillIndex, SkillVersion

DEFAULT_TARGET_BASE = ".claude/skills"
_SNAPSHOT_SENTINEL = ".aim.complete"


class SkillNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the skill index — try `repo refresh`."""


class SkillNotInstalledError(KeyError):
    """No entry for this skill in the project manifest."""


class SkillSourcePathChangedError(RuntimeError):
    """On `update`, the skill's source_path inside its repo differs from the
    installed version's. Aborts to avoid silently re-pointing the install."""


class LocalEditsError(RuntimeError):
    """The deployed target_dir has been edited by hand. Pass `force=True` to overwrite."""


class NoHistoryToRollbackError(RuntimeError):
    """The skill has no prior version recorded, so there is nothing to roll back to."""


class RollbackUnavailableError(RuntimeError):
    """Snapshot is gone AND upstream refetch failed. Loud failure as the plan accepts."""


class ManifestPathEscapeError(ValueError):
    """A manifest-stored target_dir/target_path resolves outside the project root."""


@dataclass(frozen=True)
class InstallPlan:
    """Resolved inputs needed to deploy one skill version into a project."""

    qualified_name: str
    repo_alias: str
    skill_name: str
    source_path: str
    target_dir: Path
    version: SkillVersion
    project_root: Path


def _skill_index_row(qualified_name: str) -> SkillIndex:
    """Fetch the index row for a skill, or raise if it is not indexed.

    Raises:
        SkillNotIndexedError: the qualified_name is absent from the index.
    """
    with db.session() as session:
        row = session.get(SkillIndex, qualified_name)
    if row is None:
        raise SkillNotIndexedError(qualified_name)
    return row


def _snapshot_dir(repo_alias: str, sha: str, skill_name: str) -> Path:
    """Return the cache path where this skill's snapshot bytes live."""
    return paths.snapshots_cache_dir() / repo_alias / sha / skill_name


def _snapshot_is_complete(snap: Path) -> bool:
    """Return True when the snapshot directory carries its completion sentinel."""
    return snap.exists() and (snap / _SNAPSHOT_SENTINEL).exists()


def _ensure_snapshot(repo_alias: str, sha: str, source_path: str, skill_name: str) -> Path:
    """Materialise (or reuse) the snapshot of a skill at a given SHA.

    Args:
        repo_alias: registered alias of the source repo.
        sha: commit whose bytes are extracted.
        source_path: path within the repo to extract.
        skill_name: name used for the snapshot subdirectory.

    Returns:
        The snapshot directory, guaranteed complete on success.

    Raises:
        RollbackUnavailableError: the snapshot is missing and the cached
            clone cannot be used to rebuild it.
    """
    snap = _snapshot_dir(repo_alias, sha, skill_name)
    if _snapshot_is_complete(snap):
        return snap
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True, exist_ok=True)
    repo_dir = repos.clone_dir(repo_alias)
    if not repo_dir.exists():
        raise RollbackUnavailableError(
            f"snapshot for {repo_alias}/{skill_name}@{sha[:12]} is missing "
            f"and the cached clone {repo_dir} does not exist"
        )
    try:
        git.get_backend().archive(repo_dir, sha, source_path, snap)
    except git.GitError as exc:
        shutil.rmtree(snap, ignore_errors=True)
        raise RollbackUnavailableError(
            f"could not materialise {repo_alias}/{skill_name}@{sha[:12]}: {exc}"
        ) from exc
    (snap / _SNAPSHOT_SENTINEL).write_text("")
    return snap


def resolve_install_version(
    repo_alias: str,
    source_path: str,
    *,
    track: str | None = None,
    pin: str | None = None,
    artifact_name: str = "SKILL.md",
) -> SkillVersion:
    """Pick the version to install for this artifact (skill or agent).

    The actual SHA is the last commit touching `source_path` reachable from
    the resolved ref. The most recent ancestor tag is attached IFF (a) the
    source_path exists at that tag and (b) the artifact's last-touching SHA
    is reachable from the tag (so the tag honestly describes the bytes).

    Args:
        repo_alias: registered alias of the source repo.
        source_path: path within the repo to the artifact.
        track: overrides the registered repo's default_ref (e.g. "main", a
            branch, or "latest-tag" for the newest reachable tag).
        pin: an exact tag/sha returned verbatim if it resolves, so the
            install stays put even when upstream advances.
        artifact_name: the manifest file inside source_path (SKILL.md or
            AGENT.md).

    Returns:
        The resolved version to install.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    backend = git.get_backend()

    if pin:
        # Resolve `pin` as a ref (tag preferred). If it doesn't resolve we
        # let GitError propagate so the caller surfaces it.
        pin_sha = backend.resolve_ref(repo_dir, pin)
        return SkillVersion(
            tag=pin if not pin.startswith("sha:") else None,
            sha=pin_sha,
            installed_at=datetime.now(UTC),
        )

    if track == "latest-tag":
        latest = backend.latest_tag(repo_dir, repo.default_ref)
        if latest is not None:
            tag_sha = backend.resolve_ref(repo_dir, latest)
            sha = backend.last_touching_sha(repo_dir, tag_sha, source_path)
            return SkillVersion(tag=latest, sha=sha, installed_at=datetime.now(UTC))
        # Fall through to default behaviour if no tags.

    ref = track or repo.default_ref
    head_sha = backend.resolve_ref(repo_dir, ref)
    sha = backend.last_touching_sha(repo_dir, head_sha, source_path)

    # Flat file: source_path may already point at the artifact itself
    # (e.g. agents/foo.md). Otherwise it's the directory containing it.
    if source_path.endswith(f"/{artifact_name}") or source_path.endswith(".md"):
        artifact_path = source_path
    else:
        artifact_path = f"{source_path}/{artifact_name}" if source_path else artifact_name

    tag: str | None = backend.latest_tag(repo_dir, head_sha)
    if tag is not None:
        try:
            tag_sha = backend.resolve_ref(repo_dir, tag)
            tag_paths = backend.ls_tree(repo_dir, tag_sha, source_path or "")
            has_artifact = any(
                p == artifact_path or p.endswith(f"/{artifact_name}") for p in tag_paths
            )
            if not has_artifact:
                tag = None
            else:
                # Is the install SHA an ancestor of the tag SHA?
                # `git merge-base --is-ancestor` returns 0 when yes, 1 when no.
                # We re-use last_touching_sha: the last sha touching source_path
                # reachable from tag_sha. If it equals our install sha, tag is
                # at-or-after the edit; otherwise the edit happened after the tag.
                try:
                    tag_last_touching = backend.last_touching_sha(repo_dir, tag_sha, source_path)
                    if tag_last_touching != sha:
                        tag = None
                except git.GitError:
                    tag = None
        except git.GitError:
            tag = None

    return SkillVersion(tag=tag, sha=sha, installed_at=datetime.now(UTC))


def _resolve_target_dir(project_root: Path, target_dir: str) -> Path:
    """Validate a manifest-originated target_dir and return its absolute path."""
    safe = paths.safe_project_path(project_root, target_dir)
    if safe is None:
        raise ManifestPathEscapeError(f"manifest target_dir escapes project root: {target_dir!r}")
    return safe


def _plan(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
) -> InstallPlan:
    """Build the install plan for a skill from its index row and active layout.

    Args:
        project_root: project whose layout profile and target dir apply.
        qualified_name: the skill to plan an install for.
        track: optional ref override passed to version resolution.
        pin: optional exact tag/sha passed to version resolution.

    Returns:
        A fully resolved install plan.
    """
    row = _skill_index_row(qualified_name)
    version = resolve_install_version(row.repo_alias, row.source_path, track=track, pin=pin)
    profile = layout_profiles.resolve_active(project_root)
    target_dir = _resolve_target_dir(project_root, str(Path(profile.skills_dir) / row.skill_name))
    return InstallPlan(
        qualified_name=qualified_name,
        repo_alias=row.repo_alias,
        skill_name=row.skill_name,
        source_path=row.source_path,
        target_dir=target_dir,
        version=version,
        project_root=project_root,
    )


def _ensure_symlinks_safe(snap: Path) -> None:
    """Reject symlinks whose target escapes the snapshot or is absolute."""
    snap_resolved = snap.resolve()
    for path in snap.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.readlink()
        if target.is_absolute():
            raise LocalEditsError(
                f"snapshot contains absolute symlink: {path.relative_to(snap)} -> {target}"
            )
        resolved = (path.parent / target).resolve()
        if resolved != snap_resolved and not resolved.is_relative_to(snap_resolved):
            raise LocalEditsError(
                f"snapshot contains escaping symlink: {path.relative_to(snap)} -> {target}"
            )


_RISK_TEXT_CAP = 256 * 1024  # bytes; bound the text fed to the classifier


def _repo_url(alias: str) -> str:
    """Return the registered URL for `alias`, or '' if unknown (alias match still applies)."""
    try:
        return repos.get(alias).url
    except repos.RepoNotFoundError:
        return ""


def _gather_skill_text(snap: Path) -> str:
    """Concatenate the UTF-8 text of a skill snapshot for risk classification.

    The total is capped so a large or hostile tree can't exhaust memory. Only
    called when a policy enables risk scanning.

    Args:
        snap: the snapshot directory to read files from.

    Returns:
        The concatenated text, truncated to the byte cap.
    """
    parts: list[str] = []
    total = 0
    for path in sorted(snap.rglob("*")):
        if total >= _RISK_TEXT_CAP:
            break
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        parts.append(text[: _RISK_TEXT_CAP - total])
        total += len(text)
    return "\n".join(parts)


def _deploy(plan: InstallPlan, *, override_risk: bool = False) -> str:
    """Materialise the skill bytes into the project target_dir.

    Args:
        plan: the resolved install plan to deploy.
        override_risk: bypass a risk gate the user has acknowledged.

    Returns:
        The content hash of the deployed tree.

    Raises:
        content_guard.HiddenUnicodeError: hidden Unicode found in skill files.
    """
    snap = _ensure_snapshot(plan.repo_alias, plan.version.sha, plan.source_path, plan.skill_name)
    _ensure_symlinks_safe(snap)
    pol = policy.effective_policy(plan.project_root)
    policy.assert_repo_allowed(pol, plan.repo_alias, _repo_url(plan.repo_alias))
    policy.assert_artifact_allowed(pol, "skill", plan.qualified_name)
    hidden = content_guard.scan_directory(snap)
    if hidden:
        raise content_guard.HiddenUnicodeError(
            f"{plan.qualified_name}: hidden Unicode found in skill files:\n" + "\n".join(hidden)
        )
    # Guarded because gathering skill text is risk-only work; skip it when risk is off
    # (agents/rules already hold their content, so they call risk.gate unconditionally).
    if pol.risk.active_for("skill"):
        risk.gate(
            _gather_skill_text(snap),
            qualified_name=plan.qualified_name,
            pol=pol,
            override_risk=override_risk,
            kind="skill",
        )
    if plan.target_dir.exists():
        shutil.rmtree(plan.target_dir)
    plan.target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        snap,
        plan.target_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns(_SNAPSHOT_SENTINEL),
    )
    return hashing.hash_tree(plan.target_dir)


def _load_manifest(project_root: Path) -> Manifest:
    """Load the project manifest, creating an empty one if absent."""
    return manifest.load_or_create(project_root)


def _find_installed(m: Manifest, qualified_name: str) -> InstalledSkill | None:
    """Return the manifest's installed entry for a skill, or None if absent."""
    for skill in m.skills:
        if skill.qualified_name == qualified_name:
            return skill
    return None


def install(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
    override_risk: bool = False,
) -> InstalledSkill:
    """Install or re-install a skill and record it in the project manifest.

    Args:
        project_root: project to install into.
        qualified_name: the skill to install.
        track: optional ref override for version resolution.
        pin: optional exact tag/sha to install.
        override_risk: bypass a risk gate the user has acknowledged.

    Returns:
        The manifest entry for the installed skill.
    """
    plan = _plan(project_root, qualified_name, track=track, pin=pin)

    # We don't auto-install prereqs across repos (per the plan); the user
    # gets a clear print-list to install themselves.
    _warn_about_prereqs_and_capabilities(project_root, qualified_name)

    content_hash = _deploy(plan, override_risk=override_risk)

    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        installed = InstalledSkill(
            qualified_name=qualified_name,
            repo_alias=plan.repo_alias,
            repo_url=repos.get(plan.repo_alias).url,
            source_path=plan.source_path,
            target_dir=str(plan.target_dir.relative_to(project_root)),
            current=plan.version,
            content_hash=content_hash,
            pin=pin,
            track=track,
            risk_acknowledged=override_risk,
        )
        m.skills.append(installed)
        result = installed
    else:
        existing.push_history(plan.version)
        existing.repo_alias = plan.repo_alias
        existing.source_path = plan.source_path
        existing.target_dir = str(plan.target_dir.relative_to(project_root))
        existing.content_hash = content_hash
        if override_risk:
            existing.risk_acknowledged = True
        if pin is not None:
            existing.pin = pin
        if track is not None:
            existing.track = track
        result = existing
    manifest.save(project_root, m)
    declarations._update_skill(project_root, result)
    return result


_install_warnings: list[str] = []


def take_install_warnings() -> list[str]:
    """Drain the install-warning buffer. CLI/TUI surfaces these."""
    out = list(_install_warnings)
    _install_warnings.clear()
    return out


def _warn_about_prereqs_and_capabilities(project_root: Path, qualified_name: str) -> None:
    """Surface missing prereqs and capability collisions for a skill.

    Inspects the SkillIndex row and the existing manifest. Warnings are
    drained via `take_install_warnings()`.

    Args:
        project_root: project whose installed skills are checked.
        qualified_name: the skill being installed.
    """
    from aim.core.skills import split_csv

    with db.session() as session:
        row = session.get(SkillIndex, qualified_name)
    if row is None:
        return
    prereqs = split_csv(row.prereqs or "")
    provides = split_csv(row.provides or "")

    try:
        m = manifest.load(project_root)
        installed_names = {s.qualified_name for s in m.skills}
    except manifest.ManifestNotFoundError:
        installed_names = set()

    missing = [p for p in prereqs if p != qualified_name and p not in installed_names]
    if missing:
        _install_warnings.append(
            f"{qualified_name}: missing prereqs: {', '.join(missing)}. "
            "Install them with `aim skill install <name>`."
        )

    # Capability collisions: other installed skills that already provide one
    # of this skill's capabilities.
    if provides:
        with db.session() as session:
            other_indexes = list(
                session.exec(
                    select(SkillIndex).where(SkillIndex.qualified_name != qualified_name)
                ).all()
            )
        for other in other_indexes:
            if other.qualified_name not in installed_names:
                continue
            other_provides = set(split_csv(other.provides or ""))
            overlap = other_provides & set(provides)
            if overlap:
                _install_warnings.append(
                    f"{qualified_name}: capability collision with "
                    f"{other.qualified_name} on: {', '.join(sorted(overlap))}"
                )


def _check_local_edits(project_root: Path, installed: InstalledSkill, *, force: bool) -> None:
    """Refuse to overwrite a target_dir the user has hand-edited since install.

    Args:
        project_root: project containing the deployed skill.
        installed: the manifest entry to verify against its target_dir.
        force: when True, skip the check and allow overwrite.

    Raises:
        LocalEditsError: the deployed files no longer match the stored hash.
    """
    if force or installed.content_hash is None:
        return
    target = _resolve_target_dir(project_root, installed.target_dir)
    if not target.exists():
        return
    current = hashing.hash_tree(target)
    if current != installed.content_hash:
        raise LocalEditsError(
            f"{installed.qualified_name}: files in {target} have been modified since install. "
            "Pass force=True (`--force`) to overwrite."
        )


@dataclass(frozen=True)
class UpdatePreview:
    """What `update --dry-run` would do for a single skill."""

    qualified_name: str
    current_sha: str
    proposed_sha: str
    proposed_tag: str | None
    will_change: bool


def update(
    project_root: Path,
    qualified_name: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    override_risk: bool = False,
) -> InstalledSkill | UpdatePreview:
    """Update an installed skill to the latest resolved version.

    Args:
        project_root: project containing the skill.
        qualified_name: the skill to update.
        force: overwrite even if the target_dir was hand-edited.
        dry_run: return a preview without applying changes.
        override_risk: bypass a risk gate the user has acknowledged.

    Returns:
        The updated manifest entry, or an UpdatePreview when dry_run is True.

    Raises:
        SkillNotInstalledError: the skill is not in the manifest.
        SkillSourcePathChangedError: upstream moved the skill's source_path.
    """
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise SkillNotInstalledError(qualified_name)
    row = _skill_index_row(qualified_name)
    if row.source_path != existing.source_path:
        raise SkillSourcePathChangedError(
            f"{qualified_name}: source path moved from "
            f"{existing.source_path!r} (installed) to {row.source_path!r} (upstream). "
            "Reinstall explicitly to accept the move."
        )
    new_version = resolve_install_version(
        existing.repo_alias,
        existing.source_path,
        track=existing.track,
        pin=existing.pin,
    )
    if dry_run:
        return UpdatePreview(
            qualified_name=qualified_name,
            current_sha=existing.current.sha,
            proposed_sha=new_version.sha,
            proposed_tag=new_version.tag,
            will_change=new_version.sha != existing.current.sha,
        )
    _check_local_edits(project_root, existing, force=force)
    if new_version.sha == existing.current.sha:
        return existing
    plan = InstallPlan(
        qualified_name=qualified_name,
        repo_alias=existing.repo_alias,
        skill_name=row.skill_name,
        source_path=existing.source_path,
        target_dir=_resolve_target_dir(project_root, existing.target_dir),
        version=new_version,
        project_root=project_root,
    )
    content_hash = _deploy(plan, override_risk=override_risk)
    existing.push_history(new_version)
    existing.content_hash = content_hash
    if override_risk:
        existing.risk_acknowledged = True
    manifest.save(project_root, m)
    declarations._update_skill(project_root, existing)
    return existing


@dataclass
class BulkUpdateOutcome:
    """Per-skill result of a bulk update run."""

    qualified_name: str
    status: str  # "updated" | "noop" | "skipped" | "error"
    detail: str = ""


def update_many(
    project_root: Path,
    *,
    repo_alias: str | None = None,
    only_outdated: bool = False,
    force: bool = False,
    dry_run: bool = False,
    override_risk: bool = False,
) -> list[BulkUpdateOutcome]:
    """Update all (or a filtered subset of) installed skills in a project.

    Partial failures don't half-write — the per-skill `update()` is atomic.
    A failing skill is recorded as status="error" and the run continues.

    Args:
        project_root: project whose skills are updated.
        repo_alias: limit to a single source repo.
        only_outdated: skip skills already at HEAD.
        force: pass through to per-skill update.
        dry_run: return previews without applying.
        override_risk: bypass a risk gate the user has acknowledged.

    Returns:
        One outcome per skill considered.
    """
    m = _load_manifest(project_root)
    outcomes: list[BulkUpdateOutcome] = []
    for skill in list(m.skills):
        if repo_alias is not None and skill.repo_alias != repo_alias:
            outcomes.append(BulkUpdateOutcome(skill.qualified_name, "skipped", "repo filter"))
            continue
        try:
            if only_outdated or dry_run:
                preview = update(project_root, skill.qualified_name, dry_run=True)
                assert isinstance(preview, UpdatePreview)
                if not preview.will_change:
                    outcomes.append(BulkUpdateOutcome(skill.qualified_name, "noop", "at HEAD"))
                    if dry_run or only_outdated:
                        continue
                if dry_run:
                    ident = (
                        f"{preview.proposed_tag}+{preview.proposed_sha[:7]}"
                        if preview.proposed_tag
                        else preview.proposed_sha[:7]
                    )
                    outcomes.append(
                        BulkUpdateOutcome(
                            skill.qualified_name,
                            "would-update",
                            f"{preview.current_sha[:7]} -> {ident}",
                        )
                    )
                    continue
            result = update(
                project_root, skill.qualified_name, force=force, override_risk=override_risk
            )
            assert not isinstance(result, UpdatePreview)
            outcomes.append(
                BulkUpdateOutcome(
                    skill.qualified_name,
                    "updated",
                    result.current.identifier(),
                )
            )
        except (
            SkillNotIndexedError,
            SkillSourcePathChangedError,
            LocalEditsError,
            git.GitError,
            RollbackUnavailableError,
        ) as exc:
            outcomes.append(BulkUpdateOutcome(skill.qualified_name, "error", str(exc)))
    return outcomes


def delete(project_root: Path, qualified_name: str) -> None:
    """Remove an installed skill's files and manifest entry.

    Raises:
        SkillNotInstalledError: the skill is not in the manifest.
    """
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise SkillNotInstalledError(qualified_name)
    target = _resolve_target_dir(project_root, existing.target_dir)
    if target.exists():
        shutil.rmtree(target)
    m.skills = [s for s in m.skills if s.qualified_name != qualified_name]
    manifest.save(project_root, m)
    declarations._remove_skill(project_root, qualified_name)


def rollback(project_root: Path, qualified_name: str, *, force: bool = False) -> InstalledSkill:
    """Restore `history[0]` as the new current version.

    The previously-current entry becomes the new `history[0]`, so rolling back
    twice in a row returns to where you started, by design.

    Args:
        project_root: project containing the skill.
        qualified_name: the skill to roll back.
        force: overwrite even if the target_dir was hand-edited.

    Returns:
        The updated manifest entry.

    Raises:
        SkillNotInstalledError: the skill is not in the manifest.
        NoHistoryToRollbackError: there is no prior version to restore.
    """
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise SkillNotInstalledError(qualified_name)
    if not existing.history:
        raise NoHistoryToRollbackError(qualified_name)
    _check_local_edits(project_root, existing, force=force)
    target_version = existing.history[0]

    plan = InstallPlan(
        qualified_name=qualified_name,
        repo_alias=existing.repo_alias,
        skill_name=existing.qualified_name.split("/", 1)[1],
        source_path=existing.source_path,
        target_dir=_resolve_target_dir(project_root, existing.target_dir),
        version=target_version,
        project_root=project_root,
    )
    content_hash = _deploy(plan)

    existing.push_history(
        SkillVersion(
            tag=target_version.tag,
            sha=target_version.sha,
            installed_at=datetime.now(UTC),
        )
    )
    existing.content_hash = content_hash
    manifest.save(project_root, m)
    declarations._update_skill(project_root, existing)
    return existing
