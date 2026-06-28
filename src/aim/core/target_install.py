"""Target install / update / delete / rollback.

A *target* is a declarative plugin-kind TOML sourced from a registered repo,
pinned to a SHA and content-hashed for drift detection. Unlike a rule, a target
is config (not agent-facing instructions): it is NOT risk-scanned, and it always
vendors to a fixed path — ``.aim/targets/<name>.toml`` — which is already a load
source for ``plugin_kinds.load_kinds``, so installing a target activates it.
"""

from __future__ import annotations

from pathlib import Path

from aim.core import (
    content_guard,
    declarations,
    git,
    hashing,
    manifest,
    paths,
    policy,
    repos,
    targets,
    validation,
)
from aim.core.install import resolve_install_version
from aim.core.models import InstalledTarget, Manifest, SkillVersion, TargetIndex


class TargetNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the target index — try `repo refresh`."""


class TargetNotInstalledError(KeyError):
    """No entry for this target in the project manifest."""


class TargetSourcePathChangedError(RuntimeError):
    """On `update`, the target's source_path inside its repo differs from the
    installed version's. Aborts to avoid silently re-pointing the install."""


class TargetLocalEditsError(RuntimeError):
    """The deployed target file has been edited by hand. Pass `force=True` to overwrite."""


class TargetNoHistoryToRollbackError(RuntimeError):
    """The target has no prior version recorded to roll back to."""


class TargetManifestPathEscapeError(ValueError):
    """A derived target path resolves outside the project root."""


def _index_row(qualified_name: str) -> TargetIndex:
    try:
        return targets.index_row(qualified_name)
    except targets.TargetNotIndexedError as exc:
        raise TargetNotIndexedError(qualified_name) from exc


def _target_name(qualified_name: str) -> str:
    """Strip the repo alias prefix from a qualified target name."""
    return qualified_name.split("/", 1)[1] if "/" in qualified_name else qualified_name


def _load_manifest(project_root: Path) -> Manifest:
    return manifest.load_or_create(project_root)


def _find_installed(m: Manifest, qualified_name: str) -> InstalledTarget | None:
    for t in m.targets:
        if t.qualified_name == qualified_name:
            return t
    return None


def _target_path(project_root: Path, name: str) -> Path:
    """Resolve the on-disk vendored path for a target: ``.aim/targets/<name>.toml``.

    Raises:
        ValueError: The target name is not a safe file name.
        TargetManifestPathEscapeError: The derived path escapes the project root.
    """
    if not validation.is_valid_plugin_name(name):
        raise ValueError(f"target name {name!r} is not a safe file name")
    rel = f".aim/targets/{name}.toml"
    safe = paths.safe_project_path(project_root, rel)
    if safe is None:
        raise TargetManifestPathEscapeError(f"target path escapes the project: {rel}")
    return safe


def _read_at_sha(source_path: str, repo_alias: str, sha: str) -> str:
    repo_dir = repos.clone_dir(repo_alias)
    return git.get_backend().cat_file(repo_dir, sha, source_path)


def _repo_url(alias: str) -> str:
    try:
        return repos.get(alias).url
    except repos.RepoNotFoundError:
        return ""


def _check_local_edits(project_root: Path, installed: InstalledTarget, *, force: bool) -> None:
    """Guard against overwriting a hand-edited target file."""
    if force or installed.content_hash is None:
        return
    target = _target_path(project_root, _target_name(installed.qualified_name))
    if not target.exists():
        return
    current = hashing.hash_text(target.read_text(encoding="utf-8"))
    if current != installed.content_hash:
        raise TargetLocalEditsError(
            f"{installed.qualified_name}: {target} has been modified since install. "
            "Pass force=True (`--force`) to overwrite."
        )


def _gate_target(project_root: Path, qualified_name: str, content: str) -> None:
    """Run repo-policy and content-safety checks on a target's TOML.

    Targets are not risk-scanned (they are config, not agent-facing instructions),
    but the source repo must be policy-allowed and the bytes hidden-unicode-clean.
    """
    pol = policy.effective_policy(project_root)
    alias = qualified_name.split("/", 1)[0]
    policy.assert_repo_allowed(pol, alias, _repo_url(alias))
    content_guard.assert_no_hidden_unicode(content, source=f"target {qualified_name}")


def _deploy(project_root: Path, name: str, content: str, *, qualified_name: str) -> None:
    """Gate then write the target TOML to ``.aim/targets/<name>.toml``."""
    _gate_target(project_root, qualified_name, content)
    target = _target_path(project_root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def install(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
) -> InstalledTarget:
    """Install a plugin target into the project (vendored to ``.aim/targets/``)."""
    row = _index_row(qualified_name)
    version = resolve_install_version(
        row.repo_alias,
        row.target_toml_path,
        track=track,
        pin=pin,
        artifact_name=Path(row.target_toml_path).name,
    )
    content = targets.read_target_content(qualified_name)
    _deploy(project_root, row.target_name, content, qualified_name=qualified_name)
    content_hash = hashing.hash_text(content)

    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        installed = InstalledTarget(
            qualified_name=qualified_name,
            repo_alias=row.repo_alias,
            repo_url=repos.get(row.repo_alias).url,
            source_path=row.target_toml_path,
            current=version,
            content_hash=content_hash,
            pin=pin,
            track=track,
        )
        m.targets.append(installed)
        result = installed
    else:
        existing.push_history(version)
        existing.repo_alias = row.repo_alias
        existing.source_path = row.target_toml_path
        existing.content_hash = content_hash
        if pin is not None:
            existing.pin = pin
        if track is not None:
            existing.track = track
        result = existing
    manifest.save(project_root, m)
    declarations._update_target(project_root, result)
    return result


def update(
    project_root: Path,
    qualified_name: str,
    *,
    force: bool = False,
) -> InstalledTarget:
    """Refresh an installed target from its source repo."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise TargetNotInstalledError(qualified_name)
    row = _index_row(qualified_name)
    if row.target_toml_path != existing.source_path:
        raise TargetSourcePathChangedError(
            f"{qualified_name}: source path moved from "
            f"{existing.source_path!r} (installed) to {row.target_toml_path!r} (upstream). "
            "Reinstall explicitly to accept the move."
        )
    new_version = resolve_install_version(
        existing.repo_alias,
        existing.source_path,
        track=existing.track,
        pin=existing.pin,
        artifact_name=Path(existing.source_path).name,
    )
    if new_version.sha == existing.current.sha:
        return existing

    _check_local_edits(project_root, existing, force=force)
    content = targets.read_target_content(qualified_name)
    _deploy(project_root, _target_name(qualified_name), content, qualified_name=qualified_name)
    existing.push_history(new_version)
    existing.content_hash = hashing.hash_text(content)
    manifest.save(project_root, m)
    declarations._update_target(project_root, existing)
    return existing


def update_many(
    project_root: Path,
    *,
    repo_alias: str | None = None,
    only_outdated: bool = False,
    force: bool = False,
) -> list[dict]:
    """Update all (or a filtered subset of) installed targets in a project."""
    from dataclasses import dataclass

    @dataclass
    class Outcome:
        qualified_name: str
        status: str
        detail: str = ""

    m = _load_manifest(project_root)
    outcomes: list[Outcome] = []
    for target in list(m.targets):
        if repo_alias is not None and target.repo_alias != repo_alias:
            outcomes.append(Outcome(target.qualified_name, "skipped", "repo filter"))
            continue
        try:
            if only_outdated:
                _index_row(target.qualified_name)  # ensure still indexed
                new_version = resolve_install_version(
                    target.repo_alias,
                    target.source_path,
                    track=target.track,
                    pin=target.pin,
                    artifact_name=Path(target.source_path).name,
                )
                if new_version.sha == target.current.sha:
                    outcomes.append(Outcome(target.qualified_name, "noop", "at HEAD"))
                    continue
            result = update(project_root, target.qualified_name, force=force)
            outcomes.append(Outcome(target.qualified_name, "updated", result.current.identifier()))
        except Exception as exc:
            outcomes.append(Outcome(target.qualified_name, "error", str(exc)))
    return [
        {"qualified_name": o.qualified_name, "status": o.status, "detail": o.detail}
        for o in outcomes
    ]


def delete(project_root: Path, qualified_name: str) -> None:
    """Remove an installed target file and its manifest entry."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise TargetNotInstalledError(qualified_name)
    target = _target_path(project_root, _target_name(qualified_name))
    if target.exists():
        target.unlink()
    m.targets = [t for t in m.targets if t.qualified_name != qualified_name]
    manifest.save(project_root, m)
    declarations._remove_target(project_root, qualified_name)


def rollback(project_root: Path, qualified_name: str, *, force: bool = False) -> InstalledTarget:
    """Restore ``history[0]`` as the current installed target."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise TargetNotInstalledError(qualified_name)
    if not existing.history:
        raise TargetNoHistoryToRollbackError(qualified_name)
    _check_local_edits(project_root, existing, force=force)
    target_version = existing.history[0]

    content = _read_at_sha(existing.source_path, existing.repo_alias, target_version.sha)
    _deploy(project_root, _target_name(qualified_name), content, qualified_name=qualified_name)
    existing.push_history(
        SkillVersion(
            tag=target_version.tag,
            sha=target_version.sha,
            installed_at=target_version.installed_at,
        )
    )
    existing.content_hash = hashing.hash_text(content)
    manifest.save(project_root, m)
    declarations._update_target(project_root, existing)
    return existing
