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
        return Manifest.model_validate(migrated)

    legacy_path = paths.project_manifest_path(project_root)
    if legacy_path.exists():
        raw = json.loads(legacy_path.read_text())
        migrated = migrate(raw)
        manifest = Manifest.model_validate(migrated)
        # One-time migration: write the TOML lockfile and remove the stale JSON.
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
    data = manifest.model_dump(mode="json", exclude_none=True)
    text = tomli_w.dumps(data)
    text = _singularize_table_headers(text)
    path.write_text(text + "\n", encoding="utf-8")
