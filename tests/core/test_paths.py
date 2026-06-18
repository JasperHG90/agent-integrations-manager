from __future__ import annotations

from pathlib import Path

from aim.core import paths


def test_global_dirs_resolve_under_home_override(home: Path) -> None:
    assert paths.user_data_dir() == home / "data"
    assert paths.user_cache_dir() == home / "cache"
    assert paths.user_config_dir() == home / "config"


def test_derived_paths(home: Path) -> None:
    assert paths.db_path() == home / "data" / "aim.sqlite"
    assert paths.repos_cache_dir() == home / "cache" / "repos"
    assert paths.snapshots_cache_dir() == home / "cache" / "snapshots"
    assert paths.templates_library_dir() == home / "config" / "templates"


def test_ensure_global_dirs_creates_them(home: Path) -> None:
    # home fixture already calls ensure_global_dirs once
    for sub in ("data", "cache", "cache/repos", "cache/snapshots", "config", "config/templates"):
        assert (home / sub).is_dir(), f"expected {sub} to exist"


def test_project_paths(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    assert paths.project_aim_dir(proj) == proj / ".aim"
    assert paths.project_rules_dir(proj) == proj / ".aim" / "rules"
    # Legacy JSON manifest path remains under the old .atm dir for one-time migration.
    assert paths.project_manifest_path(proj) == proj / ".atm" / "manifest.json"
