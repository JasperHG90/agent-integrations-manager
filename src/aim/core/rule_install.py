"""Rule install / update / delete / rollback.

Mirrors the sub-agent lifecycle: a rule is a single Markdown file sourced from a
registered repo, pinned to a SHA, and content-hashed for drift detection. The
render target depends on the active layout profile:

- files mode (e.g. claude): the rule body is written to `<rules_dir>/<name>.md`
  and drift-checked before overwrite.
- inline mode (e.g. opencode): the rule body is composed into AGENTS.md regions
  by `agent_files.write_agent_files`; there is no per-rule file, so the file
  write and drift check are skipped (region drift is tracked separately via
  `managed_region_hashes`).
"""

from __future__ import annotations

from pathlib import Path

from aim.core import (
    content_guard,
    declarations,
    git,
    hashing,
    layout_profiles,
    manifest,
    paths,
    policy,
    repo_rules,
    repos,
    risk,
    validation,
)
from aim.core.install import resolve_install_version
from aim.core.models import InstalledRule, Manifest, RuleIndex, SkillVersion


class RuleNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the rule index — try `repo refresh`."""


class RuleNotInstalledError(KeyError):
    """No entry for this rule in the project manifest."""


class RuleSourcePathChangedError(RuntimeError):
    """On `update`, the rule's source_path inside its repo differs from the
    installed version's. Aborts to avoid silently re-pointing the install."""


class RuleLocalEditsError(RuntimeError):
    """The deployed rule file has been edited by hand. Pass `force=True` to overwrite."""


class RuleNoHistoryToRollbackError(RuntimeError):
    pass


class RuleManifestPathEscapeError(ValueError):
    """A derived rule target path resolves outside the project root."""


def _rule_index_row(qualified_name: str) -> RuleIndex:
    try:
        return repo_rules.index_row(qualified_name)
    except repo_rules.RuleNotIndexedError as exc:
        raise RuleNotIndexedError(qualified_name) from exc


def _rule_name(qualified_name: str) -> str:
    return qualified_name.split("/", 1)[1] if "/" in qualified_name else qualified_name


def _load_manifest(project_root: Path) -> Manifest:
    return manifest.load_or_create(project_root)


def _find_installed(m: Manifest, qualified_name: str) -> InstalledRule | None:
    for r in m.rules:
        if r.qualified_name == qualified_name:
            return r
    return None


def _target_path(project_root: Path, rule_name: str) -> Path | None:
    """Resolve the on-disk target for files mode, or None in inline mode."""
    profile = layout_profiles.resolve_active(project_root)
    if profile.rules_mode != "files":
        return None
    if not validation.is_valid_rule_name(rule_name):
        raise ValueError(f"rule name {rule_name!r} is not a safe file name")
    rel = f"{profile.rules_dir}/{rule_name}.md"
    safe = paths.safe_project_path(project_root, rel)
    if safe is None:
        raise RuleManifestPathEscapeError(f"rule target path escapes the project: {rel}")
    return safe


def _read_at_sha(source_path: str, repo_alias: str, sha: str) -> str:
    repo_dir = repos.clone_dir(repo_alias)
    return git.get_backend().cat_file(repo_dir, sha, source_path)


def _check_local_edits(project_root: Path, installed: InstalledRule, *, force: bool) -> None:
    if force or installed.content_hash is None:
        return
    target = _target_path(project_root, _rule_name(installed.qualified_name))
    if target is None or not target.exists():
        return
    current = hashing.hash_text(target.read_text(encoding="utf-8"))
    if current != installed.content_hash:
        raise RuleLocalEditsError(
            f"{installed.qualified_name}: {target} has been modified since install. "
            "Pass force=True (`--force`) to overwrite."
        )


def _repo_url(alias: str) -> str:
    try:
        return repos.get(alias).url
    except repos.RepoNotFoundError:
        return ""


def _gate_rule(
    project_root: Path, qualified_name: str, content: str, *, override_risk: bool = False
) -> None:
    """Single content gate for rules: every deploy path (install/update/rollback/
    sync, files and inline mode) funnels through here, so security/policy/risk
    checks live in one place."""
    pol = policy.effective_policy(project_root)
    alias = qualified_name.split("/", 1)[0]
    policy.assert_repo_allowed(pol, alias, _repo_url(alias))
    policy.assert_artifact_allowed(pol, "rule", qualified_name)
    content_guard.assert_no_hidden_unicode(content, source=f"rule {qualified_name}")
    risk.gate(content, qualified_name=qualified_name, pol=pol, override_risk=override_risk)


def _deploy(
    project_root: Path,
    rule_name: str,
    content: str,
    *,
    qualified_name: str,
    override_risk: bool = False,
) -> None:
    """Gate then write the rule body to its files-mode target. The gate runs above
    the inline-mode early return, so inline rules are gated too."""
    _gate_rule(project_root, qualified_name, content, override_risk=override_risk)
    target = _target_path(project_root, rule_name)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def install(
    project_root: Path,
    qualified_name: str,
    *,
    track: str | None = None,
    pin: str | None = None,
    override_risk: bool = False,
) -> InstalledRule:
    """Install a rule into the project."""
    row = _rule_index_row(qualified_name)
    version = resolve_install_version(
        row.repo_alias,
        row.rule_md_path,
        track=track,
        pin=pin,
        artifact_name=Path(row.rule_md_path).name,
    )
    content = repo_rules.read_rule_content(qualified_name)
    _deploy(
        project_root,
        row.rule_name,
        content,
        qualified_name=qualified_name,
        override_risk=override_risk,
    )
    content_hash = hashing.hash_text(content)

    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        installed = InstalledRule(
            qualified_name=qualified_name,
            repo_alias=row.repo_alias,
            repo_url=repos.get(row.repo_alias).url,
            source_path=row.rule_md_path,
            current=version,
            content_hash=content_hash,
            pin=pin,
            track=track,
        )
        m.rules.append(installed)
        result = installed
    else:
        existing.push_history(version)
        existing.repo_alias = row.repo_alias
        existing.source_path = row.rule_md_path
        existing.content_hash = content_hash
        if pin is not None:
            existing.pin = pin
        if track is not None:
            existing.track = track
        result = existing
    manifest.save(project_root, m)
    declarations._update_rule(project_root, result)
    return result


def update(
    project_root: Path,
    qualified_name: str,
    *,
    force: bool = False,
    override_risk: bool = False,
) -> InstalledRule:
    """Refresh an installed rule from its source repo."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise RuleNotInstalledError(qualified_name)
    row = _rule_index_row(qualified_name)
    if row.rule_md_path != existing.source_path:
        raise RuleSourcePathChangedError(
            f"{qualified_name}: source path moved from "
            f"{existing.source_path!r} (installed) to {row.rule_md_path!r} (upstream). "
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
    content = repo_rules.read_rule_content(qualified_name)
    _deploy(
        project_root,
        _rule_name(qualified_name),
        content,
        qualified_name=qualified_name,
        override_risk=override_risk,
    )
    existing.push_history(new_version)
    existing.content_hash = hashing.hash_text(content)
    manifest.save(project_root, m)
    declarations._update_rule(project_root, existing)
    return existing


def update_many(
    project_root: Path,
    *,
    repo_alias: str | None = None,
    only_outdated: bool = False,
    force: bool = False,
) -> list[dict]:
    """Update all (or a filtered subset of) installed rules in a project."""
    from dataclasses import dataclass

    @dataclass
    class Outcome:
        qualified_name: str
        status: str
        detail: str = ""

    m = _load_manifest(project_root)
    outcomes: list[Outcome] = []
    for rule in list(m.rules):
        if repo_alias is not None and rule.repo_alias != repo_alias:
            outcomes.append(Outcome(rule.qualified_name, "skipped", "repo filter"))
            continue
        try:
            if only_outdated:
                _rule_index_row(rule.qualified_name)  # ensure still indexed
                new_version = resolve_install_version(
                    rule.repo_alias,
                    rule.source_path,
                    track=rule.track,
                    pin=rule.pin,
                    artifact_name=Path(rule.source_path).name,
                )
                if new_version.sha == rule.current.sha:
                    outcomes.append(Outcome(rule.qualified_name, "noop", "at HEAD"))
                    continue
            result = update(project_root, rule.qualified_name, force=force)
            outcomes.append(Outcome(rule.qualified_name, "updated", result.current.identifier()))
        except Exception as exc:
            outcomes.append(Outcome(rule.qualified_name, "error", str(exc)))
    return [
        {"qualified_name": o.qualified_name, "status": o.status, "detail": o.detail}
        for o in outcomes
    ]


def delete(project_root: Path, qualified_name: str) -> None:
    """Remove an installed rule file (files mode) and its manifest entry."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise RuleNotInstalledError(qualified_name)
    target = _target_path(project_root, _rule_name(qualified_name))
    if target is not None and target.exists():
        target.unlink()
    m.rules = [r for r in m.rules if r.qualified_name != qualified_name]
    manifest.save(project_root, m)
    declarations._remove_rule(project_root, qualified_name)


def rollback(project_root: Path, qualified_name: str, *, force: bool = False) -> InstalledRule:
    """Restore `history[0]` as the current installed rule."""
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name)
    if existing is None:
        raise RuleNotInstalledError(qualified_name)
    if not existing.history:
        raise RuleNoHistoryToRollbackError(qualified_name)
    _check_local_edits(project_root, existing, force=force)
    target_version = existing.history[0]

    content = _read_at_sha(existing.source_path, existing.repo_alias, target_version.sha)
    _deploy(project_root, _rule_name(qualified_name), content, qualified_name=qualified_name)
    existing.push_history(
        SkillVersion(
            tag=target_version.tag,
            sha=target_version.sha,
            installed_at=target_version.installed_at,
        )
    )
    existing.content_hash = hashing.hash_text(content)
    manifest.save(project_root, m)
    declarations._update_rule(project_root, existing)
    return existing
