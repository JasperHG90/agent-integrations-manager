"""Manifest schema migration. Each version has a forward migrator to N+1.

Add a new entry to MIGRATIONS when bumping manifest_version. Migrations are
additive — non-destructive transforms only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aim.core.models import CURRENT_MANIFEST_VERSION


class ManifestVersionError(ValueError):
    pass


def _v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
    raw["manifest_version"] = 1
    return raw


def _v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """v2 adds optional `pin` and `track` to each skill. Additive only."""
    for skill in raw.get("skills", []):
        skill.setdefault("pin", None)
        skill.setdefault("track", None)
    raw["manifest_version"] = 2
    return raw


def _v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    """v3 adds optional `layout_profile`. Additive only."""
    raw.setdefault("layout_profile", None)
    raw["manifest_version"] = 3
    return raw


def _v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    """v4 adds optional `mcp_servers` and `agents` lists. Additive only."""
    raw.setdefault("mcp_servers", [])
    raw.setdefault("agents", [])
    raw["manifest_version"] = 4
    return raw


def _v4_to_v5(raw: dict[str, Any]) -> dict[str, Any]:
    """v5 adds explicit `symlinks` list. Additive only."""
    raw.setdefault("symlinks", [])
    raw["manifest_version"] = 5
    return raw


def _v5_to_v6(raw: dict[str, Any]) -> dict[str, Any]:
    """v6 adds optional `overrides` to each MCP server/version. Additive only."""
    for mcp in raw.get("mcp_servers", []):
        mcp.setdefault("overrides", None)
        for version in mcp.get("history", []):
            version.setdefault("overrides", None)
    raw["manifest_version"] = 6
    return raw


def _v6_to_v7(raw: dict[str, Any]) -> dict[str, Any]:
    """v7 drops the per-project `agent_dialect` field."""
    raw.pop("agent_dialect", None)
    raw["manifest_version"] = 7
    return raw


def _v7_to_v8(raw: dict[str, Any]) -> dict[str, Any]:
    """v8 makes rules repo-sourced, SHA-pinned artifacts. The pre-v8 manifest
    stored rules as a bare name list against a local library that no longer
    exists. Rule-less projects upgrade cleanly; projects that locked rules by
    name must re-add them (there is no automatic migration)."""
    rules = raw.get("rules")
    if rules:
        raise ManifestVersionError(
            "aim.lock.toml locks rules by name (pre-v8). v8 makes rules repo-sourced. "
            "Re-add each rule via `aim rule add <git-url> <name>`, then re-run `aim lock`."
        )
    raw["rules"] = []
    raw["manifest_version"] = 8
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
}


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
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
