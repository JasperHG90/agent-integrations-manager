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
