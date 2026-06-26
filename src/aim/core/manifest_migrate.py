"""Manifest schema migration. Each version has a forward migrator to N+1.

Add a new entry to MIGRATIONS when bumping manifest_version. Migrations are
additive — non-destructive transforms only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aim.core.models import CURRENT_MANIFEST_VERSION


class ManifestVersionError(ValueError):
    """Raise when a manifest version is unknown, too new, or unmigratable."""


def _v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v0 manifest forward to v1.

    Args:
        raw: The decoded manifest mapping at version 0.

    Returns:
        The same mapping, mutated in place, stamped at version 1.
    """
    raw["manifest_version"] = 1
    return raw


def _v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v1 manifest forward to v2.

    v2 adds optional `pin` and `track` to each skill. Additive only.

    Args:
        raw: The decoded manifest mapping at version 1.

    Returns:
        The same mapping, mutated in place, stamped at version 2.
    """
    for skill in raw.get("skills", []):
        skill.setdefault("pin", None)
        skill.setdefault("track", None)
    raw["manifest_version"] = 2
    return raw


def _v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v2 manifest forward to v3.

    v3 adds optional `layout_profile`. Additive only.

    Args:
        raw: The decoded manifest mapping at version 2.

    Returns:
        The same mapping, mutated in place, stamped at version 3.
    """
    raw.setdefault("layout_profile", None)
    raw["manifest_version"] = 3
    return raw


def _v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v3 manifest forward to v4.

    v4 adds optional `mcp_servers` and `agents` lists. Additive only.

    Args:
        raw: The decoded manifest mapping at version 3.

    Returns:
        The same mapping, mutated in place, stamped at version 4.
    """
    raw.setdefault("mcp_servers", [])
    raw.setdefault("agents", [])
    raw["manifest_version"] = 4
    return raw


def _v4_to_v5(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v4 manifest forward to v5.

    v5 adds explicit `symlinks` list. Additive only.

    Args:
        raw: The decoded manifest mapping at version 4.

    Returns:
        The same mapping, mutated in place, stamped at version 5.
    """
    raw.setdefault("symlinks", [])
    raw["manifest_version"] = 5
    return raw


def _v5_to_v6(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v5 manifest forward to v6.

    v6 adds optional `overrides` to each MCP server/version. Additive only.

    Args:
        raw: The decoded manifest mapping at version 5.

    Returns:
        The same mapping, mutated in place, stamped at version 6.
    """
    for mcp in raw.get("mcp_servers", []):
        mcp.setdefault("overrides", None)
        for version in mcp.get("history", []):
            version.setdefault("overrides", None)
    raw["manifest_version"] = 6
    return raw


def _v6_to_v7(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v6 manifest forward to v7.

    v7 drops the per-project `agent_dialect` field.

    Args:
        raw: The decoded manifest mapping at version 6.

    Returns:
        The same mapping, mutated in place, stamped at version 7.
    """
    raw.pop("agent_dialect", None)
    raw["manifest_version"] = 7
    return raw


def _v7_to_v8(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v7 manifest forward to v8.

    v8 makes rules repo-sourced, SHA-pinned artifacts. The pre-v8 manifest
    stored rules as a bare name list against a local library that no longer
    exists. Rule-less projects upgrade cleanly; projects that locked rules by
    name must re-add them (there is no automatic migration).

    Args:
        raw: The decoded manifest mapping at version 7.

    Returns:
        The same mapping, mutated in place, stamped at version 8.

    Raises:
        ManifestVersionError: If the manifest locks rules by name, since
            those cannot be migrated automatically and must be re-added.
    """
    rules = raw.get("rules")
    if rules:
        raise ManifestVersionError(
            "aim.lock.toml locks rules by name (pre-v8). v8 makes rules repo-sourced. "
            "Re-add each rule via `aim rule add <git-url> <name>`, then re-run `aim lock`."
        )
    raw["rules"] = []
    raw["manifest_version"] = 8
    return raw


def _v8_to_v9(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v8 manifest forward to v9.

    v9 adds optional `policy_repo`/`policy_hash` pinning the governing policy.
    Additive only.

    Args:
        raw: The decoded manifest mapping at version 8.

    Returns:
        The same mapping, mutated in place, stamped at version 9.
    """
    raw.setdefault("policy_repo", None)
    raw.setdefault("policy_hash", None)
    raw["manifest_version"] = 9
    return raw


def _v9_to_v10(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v9 manifest forward to v10.

    v10 adds the optional org policy commit SHA `policy_ref`. Additive only.

    Args:
        raw: The decoded manifest mapping at version 9.

    Returns:
        The same mapping, mutated in place, stamped at version 10.
    """
    raw.setdefault("policy_ref", None)
    raw["manifest_version"] = 10
    return raw


def _v10_to_v11(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v10 manifest forward to v11.

    v11 adds the optional locked instruction archetype. Additive only — absence
    means the project's AGENTS.md uses the built-in instruction template.

    Args:
        raw: The decoded manifest mapping at version 10.

    Returns:
        The same mapping, mutated in place, stamped at version 11.
    """
    raw.setdefault("instruction_archetype", None)
    raw["manifest_version"] = 11
    return raw


def _v11_to_v12(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v11 manifest forward to v12.

    v12 adds the optional applied-template pin (`template_repo`,
    `template_qualified_name`, `template_ref`, `template_hash`). Additive only.

    Args:
        raw: The decoded manifest mapping at version 11.

    Returns:
        The same mapping, mutated in place, stamped at version 12.
    """
    raw.setdefault("template_repo", None)
    raw.setdefault("template_qualified_name", None)
    raw.setdefault("template_ref", None)
    raw.setdefault("template_hash", None)
    raw["manifest_version"] = 12
    return raw


def _v12_to_v13(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v12 manifest forward to v13.

    v13 drops the vestigial `instruction_template` field. The AGENTS.md base is
    the locked instruction archetype, or the built-in default when none is set.

    Args:
        raw: The decoded manifest mapping at version 12.

    Returns:
        The same mapping, mutated in place, stamped at version 13.
    """
    raw.pop("instruction_template", None)
    raw["manifest_version"] = 13
    return raw


def _v13_to_v14(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v13 manifest forward to v14.

    v14 renames the locked `instruction_archetype` field to `archetype`.

    Args:
        raw: The decoded manifest mapping at version 13.

    Returns:
        The same mapping, mutated in place, stamped at version 14.
    """
    if "instruction_archetype" in raw:
        raw["archetype"] = raw.pop("instruction_archetype")
    raw["manifest_version"] = 14
    return raw


def _v14_to_v15(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v14 manifest forward to v15.

    v15 adds the optional `plugins` list (the plugins surface). Additive only.

    Args:
        raw: The decoded manifest mapping at version 14.

    Returns:
        The same mapping, mutated in place, stamped at version 15.
    """
    raw.setdefault("plugins", [])
    raw["manifest_version"] = 15
    return raw


def _v15_to_v16(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v15 manifest forward to v16.

    v16 makes the on-disk repo identity source-agnostic. A synthetic `[repos]`
    table (``repo_id -> normalized_url``) is derived from the artifacts' alias+url,
    every artifact's `qualified_name`/`repo_alias` is rewritten to id form, the
    per-artifact `repo_url` is dropped (re-derived on load), and each claude
    plugin's aim-local `marketplace_name`/`target_dir` are rewritten to the
    ``aim-<repo_id>`` form. Deterministic so teammates' first-run rewrites converge.

    Args:
        raw: The decoded manifest mapping at version 15.

    Returns:
        The same mapping, mutated in place, stamped at version 16.
    """
    from aim.core.policy import normalize_repo_url, repo_id_for_url

    artifact_keys = ("skills", "agents", "rules", "plugins")
    # Build repo_id -> normalized_url from every artifact's (repo_alias, repo_url).
    alias_to_id: dict[str, str] = {}
    repos: dict[str, str] = {}

    def _record(entry: dict[str, Any]) -> None:
        alias = entry.get("repo_alias")
        url = entry.get("repo_url")
        if isinstance(alias, str) and isinstance(url, str):
            rid = repo_id_for_url(url)
            alias_to_id[alias] = rid
            repos[rid] = normalize_repo_url(url)

    for key in artifact_keys:
        for entry in raw.get(key, []) or []:
            _record(entry)
    archetype = raw.get("archetype")
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _record(archetype)

    def _rewrite(entry: dict[str, Any], *, is_plugin: bool) -> None:
        alias = entry.get("repo_alias")
        rid = alias_to_id.get(alias) if isinstance(alias, str) else None
        entry.pop("repo_url", None)
        if rid is None:
            return
        entry["repo_alias"] = rid
        qn = entry.get("qualified_name")
        if isinstance(qn, str) and "/" in qn:
            name = qn.split("/", 1)[1]
            entry["qualified_name"] = f"{rid}/{name}"
        else:
            name = None
        if is_plugin:
            mkt = entry.get("marketplace_name")
            if isinstance(mkt, str):  # claude: aim-local marketplace -> id form
                entry["marketplace_name"] = f"aim-{rid}"
                if name is not None:
                    entry["target_dir"] = f".claude/plugins/aim-{rid}/{name}"

    for key in artifact_keys:
        for entry in raw.get(key, []) or []:
            _rewrite(entry, is_plugin=(key == "plugins"))
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _rewrite(archetype, is_plugin=False)

    raw["repos"] = {rid: repos[rid] for rid in sorted(repos)}
    raw["manifest_version"] = 16
    return raw


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _v0_to_v1,
    1: _v1_to_v2,
    2: _v2_to_v3,
    3: _v3_to_v4,
    4: _v4_to_v5,
    5: _v5_to_v6,
    6: _v6_to_v7,
    7: _v7_to_v8,
    8: _v8_to_v9,
    9: _v9_to_v10,
    10: _v10_to_v11,
    11: _v11_to_v12,
    12: _v12_to_v13,
    13: _v13_to_v14,
    14: _v14_to_v15,
    15: _v15_to_v16,
}


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a raw manifest to the current schema version.

    Applies forward migrators in sequence until the manifest reaches
    CURRENT_MANIFEST_VERSION. A missing `manifest_version` is treated as 0.

    Args:
        raw: The decoded manifest mapping at any supported version.

    Returns:
        The migrated mapping stamped at the current manifest version.

    Raises:
        ManifestVersionError: If `manifest_version` is not an int, is newer
            than supported, or has no registered migration path.
    """
    version = raw.get("manifest_version", 0)
    if not isinstance(version, int):
        raise ManifestVersionError(f"manifest_version must be int, got {type(version).__name__}")
    if version > CURRENT_MANIFEST_VERSION:
        raise ManifestVersionError(
            f"manifest_version {version} is newer than supported ({CURRENT_MANIFEST_VERSION}). "
            "Upgrade aim."
        )
    while version < CURRENT_MANIFEST_VERSION:
        migrator = MIGRATIONS.get(version)
        if migrator is None:
            raise ManifestVersionError(f"no migration from manifest_version {version}")
        raw = migrator(raw)
        version = raw["manifest_version"]
    return raw
