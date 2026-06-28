"""Read/merge/write Claude Code's project ``.claude/settings.json`` for plugins.

aim manages exactly two keys — ``extraKnownMarketplaces`` and
``enabledPlugins`` — and preserves every other key (e.g. ``hooks``). It registers
a **local-directory marketplace** pointing at aim's vendored copy, so Claude
loads the pinned bytes without fetching upstream.

The target file is passed in (``settings_file``, a repo-root-relative path) — the
caller (the claude kind) owns that path, so nothing here depends on a layout
profile. Modeled on the preserve-unmanaged-keys discipline of the ``.mcp.json``
writer (``mcp_registry``). Keys are written without reordering the user's existing
content (``settings.json`` is hand-edited far more than ``.mcp.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

from aim.core import content_guard


class SettingsJsonError(ValueError):
    """Raised when the settings file is present but not a JSON object."""


def read_settings(project_root: Path, settings_file: str) -> dict:
    """Read the settings file, returning an empty dict when absent or empty.

    Raises:
        SettingsJsonError: The file exists but is invalid JSON or not an object.
    """
    path = project_root / settings_file
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsJsonError(f"invalid {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsJsonError(f"{path} must contain a JSON object")
    return data


def write_settings(project_root: Path, settings_file: str, data: dict) -> Path:
    """Write the settings file with stable 2-space indentation, preserving key order."""
    path = project_root / settings_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def register(
    project_root: Path,
    *,
    settings_file: str,
    marketplace_name: str,
    marketplace_path: str,
    plugin_names: list[str],
) -> Path:
    """Register a local-directory marketplace and enable the given plugins.

    Sets ``extraKnownMarketplaces[marketplace_name]`` to a directory source at
    ``marketplace_path`` (repo-root-relative) and flips
    ``enabledPlugins["<plugin>@<marketplace_name>"]`` true for each plugin. Other
    keys are preserved. The composed enablement keys, marketplace name, and path
    are scanned for hidden Unicode (they derive from untrusted repo content).
    """
    content_guard.assert_no_hidden_unicode(marketplace_name, source="marketplace name")
    content_guard.assert_no_hidden_unicode(marketplace_path, source="marketplace path")
    data = read_settings(project_root, settings_file)

    mkts = data.setdefault("extraKnownMarketplaces", {})
    if not isinstance(mkts, dict):
        raise SettingsJsonError("extraKnownMarketplaces must be a JSON object")
    mkts[marketplace_name] = {"source": {"source": "directory", "path": marketplace_path}}

    enabled = data.setdefault("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise SettingsJsonError("enabledPlugins must be a JSON object")
    desired = {f"{name}@{marketplace_name}" for name in plugin_names}
    for key in desired:
        content_guard.assert_no_hidden_unicode(key, source="enabledPlugins key")
        enabled[key] = True
    # Reconcile: drop stale enablements for this marketplace that are no longer
    # vendored, so a removed/blocked plugin can't linger enabled in settings.json.
    suffix = f"@{marketplace_name}"
    for key in [
        k for k in enabled if isinstance(k, str) and k.endswith(suffix) and k not in desired
    ]:
        del enabled[key]

    return write_settings(project_root, settings_file, data)


def unregister(
    project_root: Path,
    *,
    settings_file: str,
    marketplace_name: str,
    plugin_name: str,
) -> Path:
    """Disable a plugin and drop its marketplace entry once nothing references it.

    Removes ``enabledPlugins["<plugin>@<marketplace>"]``; if no remaining
    ``enabledPlugins`` key references ``@<marketplace>``, the
    ``extraKnownMarketplaces`` entry is removed too (refcount).
    """
    data = read_settings(project_root, settings_file)
    enabled = data.get("enabledPlugins")
    if isinstance(enabled, dict):
        enabled.pop(f"{plugin_name}@{marketplace_name}", None)
        suffix = f"@{marketplace_name}"
        still_used = any(isinstance(k, str) and k.endswith(suffix) for k in enabled)
        if not still_used:
            mkts = data.get("extraKnownMarketplaces")
            if isinstance(mkts, dict):
                mkts.pop(marketplace_name, None)
    return write_settings(project_root, settings_file, data)


def prune_marketplaces(
    project_root: Path, *, settings_file: str, keep: set[str], path_prefix: str
) -> Path:
    """Drop managed marketplace entries no longer in ``keep``, plus their plugin
    enablements. Only entries whose ``source.path`` starts with ``path_prefix``
    (aim-vendored marketplaces) are considered; user-added marketplaces are left
    untouched. Lets an upgrade (id-form key replaced by the semantic name) or a
    name collision (semantic key demoted to id-form) self-heal to one clean set.
    """
    data = read_settings(project_root, settings_file)
    mkts = data.get("extraKnownMarketplaces")
    if not isinstance(mkts, dict):
        return write_settings(project_root, settings_file, data)

    def _managed(entry: object) -> bool:
        src = entry.get("source") if isinstance(entry, dict) else None
        path = src.get("path") if isinstance(src, dict) else None
        return isinstance(path, str) and path.startswith(path_prefix)

    stale = [name for name, entry in mkts.items() if name not in keep and _managed(entry)]
    for name in stale:
        mkts.pop(name, None)
    enabled = data.get("enabledPlugins")
    if isinstance(enabled, dict):
        for name in stale:
            suffix = f"@{name}"
            for key in [k for k in enabled if isinstance(k, str) and k.endswith(suffix)]:
                del enabled[key]
    return write_settings(project_root, settings_file, data)
