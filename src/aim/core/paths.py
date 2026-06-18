"""Platform-aware paths for aim's global state and per-project state.

Global state lives under platformdirs; per-project state lives under .aim/
inside the project root.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "aim"

_PROJECT_DIR_ENV = "AIM_HOME"


def _dirs() -> PlatformDirs:
    return PlatformDirs(appname=APP_NAME, appauthor=False, ensure_exists=False)


def user_data_dir() -> Path:
    override = os.environ.get(_PROJECT_DIR_ENV)
    if override:
        return Path(override) / "data"
    return Path(_dirs().user_data_dir)


def user_cache_dir() -> Path:
    override = os.environ.get(_PROJECT_DIR_ENV)
    if override:
        return Path(override) / "cache"
    return Path(_dirs().user_cache_dir)


def user_config_dir() -> Path:
    override = os.environ.get(_PROJECT_DIR_ENV)
    if override:
        return Path(override) / "config"
    return Path(_dirs().user_config_dir)


def db_path() -> Path:
    return user_data_dir() / "aim.sqlite"


def repos_cache_dir() -> Path:
    return user_cache_dir() / "repos"


def snapshots_cache_dir() -> Path:
    return user_cache_dir() / "snapshots"


def templates_library_dir() -> Path:
    return user_config_dir() / "templates"


def project_aim_dir(project_root: Path) -> Path:
    return project_root / ".aim"


def project_declarations_path(project_root: Path) -> Path:
    return project_root / "aim.toml"


def project_lock_path(project_root: Path) -> Path:
    return project_root / "aim.lock.toml"


def project_manifest_path(project_root: Path) -> Path:
    """Legacy JSON manifest path. Kept only for migration fallback."""
    return project_root / ".atm" / "manifest.json"


def project_rules_dir(project_root: Path) -> Path:
    return project_aim_dir(project_root) / "rules"


def project_layout_profiles_dir(project_root: Path) -> Path:
    return project_aim_dir(project_root) / "layout-profiles"


def safe_project_path(project_root: Path, rel: str, *extra: str) -> Path | None:
    """Resolve a relative project path and ensure it stays inside the project.

    Returns None if the resolved path escapes the project root or if resolution
    fails. The project root itself is considered out of bounds so that empty or
    `..`-only relative paths are rejected.
    """
    try:
        base = project_root.resolve()
        target = (base / rel / "/".join(extra)).resolve()
        if target != base and target.is_relative_to(base):
            return target
    except (ValueError, OSError):
        pass
    return None


def ensure_global_dirs() -> None:
    for path in (
        user_data_dir(),
        user_cache_dir(),
        user_config_dir(),
        repos_cache_dir(),
        snapshots_cache_dir(),
        templates_library_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
