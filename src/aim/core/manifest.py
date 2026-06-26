"""Per-project manifest read/write. The manifest is the source of truth for
installed-skill state and history; the global DB is a cache only.

The committed manifest is a TOML lockfile named `aim.lock.toml` at the project root.
Older `.atm/manifest.json` files are still readable as a one-time migration.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from aim.core import paths
from aim.core.manifest_migrate import migrate
from aim.core.models import Manifest

# TOML uses singular array-of-table headers; the models use plural field names.
_TOML_READ_MAP = {
    "skill": "skills",
    "subagent": "agents",
    "mcp_server": "mcp_servers",
    "rule": "rules",
    "plugin": "plugins",
}
_TOML_WRITE_MAP = {v: k for k, v in _TOML_READ_MAP.items()}

# Match TOML table headers like [[skills]], [skills.current], [[skills.history]], etc.
_TABLE_HEADER_RE = re.compile(r"^(\[\[?)(\w+)((?:\.\w+)*)?(\]\]?)$")


def _singularize_table_headers(text: str) -> str:
    """Rewrite plural TOML table headers to their singular array-of-table form.

    Args:
        text: Serialized TOML text using plural model field names as headers.

    Returns:
        The TOML text with mapped headers singularized (e.g. ``[[skills]]`` to
        ``[[skill]]``); unmapped headers are left unchanged.
    """
    out: list[str] = []
    for line in text.splitlines():
        match = _TABLE_HEADER_RE.match(line.strip())
        if match:
            prefix, base, suffix, suffix_bracket = match.groups()
            singular = _TOML_WRITE_MAP.get(base)
            if singular is not None:
                line = f"{prefix}{singular}{suffix or ''}{suffix_bracket}"
        out.append(line)
    return "\n".join(out)


class ManifestNotFoundError(FileNotFoundError):
    """Raised when no manifest lockfile or legacy JSON exists for a project."""

    pass


# Artifact lists that carry a (repo_alias, repo_url) pair translated at the boundary.
_REPO_ARTIFACT_KEYS = ("skills", "agents", "rules", "plugins")


def _from_disk(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate an on-disk id-keyed manifest mapping to alias-form, in place.

    Resolves each `repo_id` in the synthetic `[repos]` table to a local alias
    (registered or default-derived, no clone), rewrites every artifact's
    `qualified_name`/`repo_alias` from id to alias form, and re-derives each
    artifact's `repo_url` (the local repo's clone URL when registered, else the
    normalized URL from disk). The synthetic `[repos]` table is consumed and
    removed, since the Manifest model has no `repos` field.

    Args:
        raw: The migrated (id-form) manifest mapping.

    Returns:
        The same mapping, mutated to alias form (without a `repos` key).
    """
    from aim.core import declarations

    disk_repos = raw.pop("repos", {}) or {}
    id_to_alias, alias_to_url = declarations._resolve_disk_repos(disk_repos)

    def _rewrite(entry: dict[str, Any]) -> None:
        repo_id = entry.get("repo_alias")
        alias = id_to_alias.get(repo_id) if isinstance(repo_id, str) else None
        if alias is None:
            return
        entry["repo_alias"] = alias
        entry["repo_url"] = alias_to_url.get(alias, disk_repos.get(repo_id, ""))
        qn = entry.get("qualified_name")
        if isinstance(qn, str) and "/" in qn:
            entry["qualified_name"] = f"{alias}/{qn.split('/', 1)[1]}"

    for key in _REPO_ARTIFACT_KEYS:
        for entry in raw.get(key, []) or []:
            _rewrite(entry)
    archetype = raw.get("archetype")
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _rewrite(archetype)
    from aim.core import policy

    # Re-derive the org-policy repo's local clone URL from the per-machine record (the
    # committed value is normalized for determinism; refresh/fetch need the local form).
    policy_repo = raw.get("policy_repo")
    if isinstance(policy_repo, str) and policy_repo:
        raw["policy_repo"] = policy.local_policy_repo_url(policy_repo)
    # Resolve the registry-backed template repo back to the local alias/URL (no clone),
    # mirroring the artifact translation, so `profile check/update` finds it locally.
    template_url = raw.get("template_repo")
    if isinstance(template_url, str) and template_url:
        from aim.core import repos as repos_mod

        repo = repos_mod.get_by_id(policy.repo_id_for_url(template_url))
        if repo is not None:
            template_alias, local_url = repo.alias, repo.url
        else:
            template_alias, local_url = repos_mod.derive_default_alias(template_url), template_url
        raw["template_repo"] = local_url
        tqn = raw.get("template_qualified_name")
        if isinstance(tqn, str) and "/" in tqn:
            raw["template_qualified_name"] = f"{template_alias}/{tqn.split('/', 1)[1]}"
    return raw


def _to_disk(manifest: Manifest) -> dict[str, Any]:
    """Serialize a manifest to an on-disk id-keyed mapping (source-agnostic).

    Adds a synthetic `[repos]` table (``repo_id -> normalize_repo_url(url)``),
    rewrites every artifact's `qualified_name`/`repo_alias` to id form, and DROPS
    the per-artifact `repo_url` (re-derived on load). The plugin `marketplace_name`
    and `target_dir` are already id-based (``aim-<repo_id>``) and pass through, so
    the committed lockfile is byte-identical across machines.

    Args:
        manifest: The in-memory (alias-form) manifest.

    Returns:
        A JSON-mode mapping ready for TOML serialization.
    """
    from aim.core import policy

    data = manifest.model_dump(mode="json", exclude_none=True)
    repos: dict[str, str] = {}

    def _rewrite(entry: dict[str, Any]) -> None:
        alias = entry.get("repo_alias")
        url = entry.pop("repo_url", None)
        if not (isinstance(alias, str) and isinstance(url, str)):
            return
        rid = policy.repo_id_for_url(url)
        repos[rid] = policy.normalize_repo_url(url)
        entry["repo_alias"] = rid
        qn = entry.get("qualified_name")
        if isinstance(qn, str) and "/" in qn:
            entry["qualified_name"] = f"{rid}/{qn.split('/', 1)[1]}"

    for key in _REPO_ARTIFACT_KEYS:
        for entry in data.get(key, []) or []:
            _rewrite(entry)
    archetype = data.get("archetype")
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _rewrite(archetype)
    # The org-policy repo is a direct clone target with no [repos] indirection: store it
    # NORMALIZED for determinism; the local clone form is re-derived on load.
    if isinstance(data.get("policy_repo"), str) and data["policy_repo"]:
        data["policy_repo"] = policy.normalize_repo_url(data["policy_repo"])
    # The project-template repo is a committed scalar with no [repos] row. It is
    # registry-backed, so give it id-form identity here and re-derive the local
    # alias/URL on load (mirroring artifacts) — the committed value is portable.
    template_url = data.get("template_repo")
    if isinstance(template_url, str) and template_url:
        template_id = policy.repo_id_for_url(template_url)
        data["template_repo"] = policy.normalize_repo_url(template_url)
        tqn = data.get("template_qualified_name")
        if isinstance(tqn, str) and "/" in tqn:
            data["template_qualified_name"] = f"{template_id}/{tqn.split('/', 1)[1]}"
    data["repos"] = {rid: repos[rid] for rid in sorted(repos)}
    return data


def load(project_root: Path) -> Manifest:
    """Load the project manifest, migrating a legacy JSON file if present.

    Args:
        project_root: Project root directory containing the manifest.

    Returns:
        The validated Manifest read from the TOML lockfile (or migrated JSON).

    Raises:
        ManifestNotFoundError: If neither a lockfile nor legacy JSON exists.
    """
    lock_path = paths.project_lock_path(project_root)
    if lock_path.exists():
        raw = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        for singular, plural in _TOML_READ_MAP.items():
            if singular in raw:
                raw[plural] = raw.pop(singular)
        migrated = migrate(raw)
        alias_form = _from_disk(migrated)
        return Manifest.model_validate(alias_form)

    legacy_path = paths.project_manifest_path(project_root)
    if legacy_path.exists():
        raw = json.loads(legacy_path.read_text())
        migrated = migrate(raw)
        manifest = Manifest.model_validate(_from_disk(migrated))
        # One-time migration: write the TOML lockfile (id-keyed via save) and remove
        # the stale JSON.
        save(project_root, manifest)
        legacy_path.unlink()
        return manifest

    raise ManifestNotFoundError(lock_path)


def load_or_default(project_root: Path) -> Manifest:
    """Load the project manifest, returning an empty Manifest if none exists.

    Args:
        project_root: Project root directory containing the manifest.

    Returns:
        The loaded Manifest, or a default empty Manifest when absent.
    """
    try:
        return load(project_root)
    except ManifestNotFoundError:
        return Manifest()


def load_or_create(project_root: Path) -> Manifest:
    """Load the existing lockfile, or seed a new Manifest from aim.toml metadata.

    Used by install/update/delete paths so that the first artifact written to
    a project still produces a lockfile with symlinks, rules, and layout profile
    copied from the user's declarations.

    Args:
        project_root: Project root directory containing the manifest.

    Returns:
        The loaded Manifest, or a newly seeded Manifest from declarations.
    """
    try:
        return load(project_root)
    except ManifestNotFoundError:
        from aim.core import declarations as declarations_mod

        decl = declarations_mod.load_or_default(project_root)
        # Rules, like skills/agents, are resolved by `lock`; a freshly seeded
        # manifest starts with none and the install path appends the new one.
        m = Manifest(
            layout_profile=decl.layout_profile,
            symlinks=list(decl.symlinks),
        )
        return m


def save(project_root: Path, manifest: Manifest) -> None:
    """Serialize the manifest to the project's TOML lockfile.

    Args:
        project_root: Project root directory to write the lockfile into.
        manifest: Manifest to serialize.
    """
    path = paths.project_lock_path(project_root)
    data = _to_disk(manifest)
    text = tomli_w.dumps(data)
    text = _singularize_table_headers(text)
    path.write_text(text + "\n", encoding="utf-8")
