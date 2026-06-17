"""Tests for layout profile TOML parsing, validation, and repo/DB sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import layout_profiles, manifest, paths


@pytest.fixture
def layout_dir(project_root: Path, home: Path) -> Path:
    d = paths.project_layout_profiles_dir(project_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_parse_round_trip() -> None:
    text = layout_profiles.render_toml(layout_profiles.BUILTIN_CLAUDE)
    parsed = layout_profiles.parse_toml(text)
    assert parsed.name == "claude"
    assert parsed.skills_dir == ".claude/skills"
    assert parsed.symlinks == ["CLAUDE.md"]


def test_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        layout_profiles.LayoutProfile(name="UpperCase", skills_dir=".claude/skills")


def test_rejects_traversal_path() -> None:
    with pytest.raises(ValueError):
        layout_profiles.LayoutProfile(name="bad", skills_dir="../skills")


def test_rejects_absolute_path() -> None:
    with pytest.raises(ValueError):
        layout_profiles.LayoutProfile(name="bad", skills_dir="/skills")


def test_rejects_invalid_symlink() -> None:
    with pytest.raises(ValueError):
        layout_profiles.LayoutProfile(name="bad", symlinks=["no_extension"])


def test_rejects_agents_md_in_symlinks() -> None:
    with pytest.raises(ValueError):
        layout_profiles.LayoutProfile(name="bad", agents_md="AGENTS.md", symlinks=["AGENTS.md"])


def test_list_profiles_includes_builtins(project_root: Path) -> None:
    profiles = layout_profiles.list_profiles(project_root)
    names = {p.name for p in profiles}
    assert "claude" in names
    assert "gemini" in names


def test_get_profile_builtin(project_root: Path) -> None:
    p = layout_profiles.get_profile(project_root, "gemini")
    assert p.skills_dir == ".gemini/skills"


def test_resolve_active_returns_claude_when_no_manifest(project_root: Path) -> None:
    assert layout_profiles.resolve_active(project_root) == layout_profiles.BUILTIN_CLAUDE


def test_resolve_active_reads_manifest(project_root: Path, layout_dir: Path) -> None:
    path = layout_dir / "custom.toml"
    profile = layout_profiles.LayoutProfile(
        name="custom",
        skills_dir=".custom/skills",
    )
    path.write_text(layout_profiles.render_toml(profile), encoding="utf-8")
    m = manifest.load_or_default(project_root)
    m.layout_profile = "custom"
    manifest.save(project_root, m)
    assert layout_profiles.resolve_active(project_root).skills_dir == ".custom/skills"


def test_save_project_profile_writes_repo_file(project_root: Path, layout_dir: Path) -> None:
    profile = layout_profiles.LayoutProfile(name="proj", skills_dir=".proj/skills")
    layout_profiles.save_project_profile(project_root, profile)
    path = layout_dir / "proj.toml"
    assert path.exists()
    assert "project" in path.read_text(encoding="utf-8")


def test_save_global_profile_writes_db_and_repo(project_root: Path, layout_dir: Path) -> None:
    profile = layout_profiles.LayoutProfile(name="global", skills_dir=".global/skills")
    layout_profiles.save_global_profile(project_root, profile)
    assert (layout_dir / "global.toml").exists()
    db_profiles = {p.name for p in layout_profiles.list_db_profiles()}
    assert "global" in db_profiles


def test_repo_profile_overrides_db_profile(project_root: Path, layout_dir: Path) -> None:
    # Write a global DB profile and a repo profile with the same name but different skills_dir.
    profile_db = layout_profiles.LayoutProfile(name="shared", skills_dir=".db/skills")
    layout_profiles.save_global_profile(project_root, profile_db)
    profile_repo = layout_profiles.LayoutProfile(name="shared", skills_dir=".repo/skills")
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / "shared.toml").write_text(
        layout_profiles.render_toml(profile_repo), encoding="utf-8"
    )
    # Repo wins.
    got = layout_profiles.get_profile(project_root, "shared")
    assert got.skills_dir == ".repo/skills"


def test_sync_demotes_edited_global_copy(project_root: Path, layout_dir: Path) -> None:
    # Start with a global profile cached in DB and a matching repo read-only copy.
    profile = layout_profiles.LayoutProfile(name="shared", skills_dir=".shared/skills")
    layout_profiles.save_global_profile(project_root, profile)
    assert layout_profiles.list_db_profiles()

    # Edit the repo copy by hand.
    path = layout_dir / "shared.toml"
    edited = layout_profiles.parse_toml(path.read_text(encoding="utf-8"))
    edited = edited.model_copy(update={"skills_dir": ".edited/skills"})
    path.write_text(layout_profiles.render_toml(edited), encoding="utf-8")

    report = layout_profiles.sync_profiles(project_root)
    assert "shared" in report.demoted
    # DB cache removed.
    assert not layout_profiles.list_db_profiles()
    # Repo file now has project scope.
    synced = layout_profiles.get_profile(project_root, "shared")
    assert synced.scope == layout_profiles.LayoutProfileScope.PROJECT
    assert synced.skills_dir == ".edited/skills"


def test_delete_global_profile_removes_db_and_repo(project_root: Path, layout_dir: Path) -> None:
    profile = layout_profiles.LayoutProfile(name="gone", skills_dir=".gone/skills")
    layout_profiles.save_global_profile(project_root, profile)
    assert (layout_dir / "gone.toml").exists()
    assert layout_profiles.list_db_profiles()
    layout_profiles.delete_global_profile(project_root, "gone")
    assert not (layout_dir / "gone.toml").exists()
    assert not layout_profiles.list_db_profiles()


def test_set_active_updates_manifest(project_root: Path, layout_dir: Path) -> None:
    profile = layout_profiles.LayoutProfile(name="active", skills_dir=".active/skills")
    layout_profiles.save_project_profile(project_root, profile)
    layout_profiles.set_active(project_root, "active")
    m = manifest.load(project_root)
    assert m.layout_profile == "active"


def test_global_default_round_trip() -> None:
    layout_profiles.set_global_default("gemini")
    assert layout_profiles.get_global_default() == "gemini"
    layout_profiles.set_global_default(None)
    assert layout_profiles.get_global_default() is None


def test_content_hash_stable() -> None:
    text = layout_profiles.render_toml(layout_profiles.BUILTIN_CLAUDE)
    h1 = layout_profiles.content_hash(text)
    h2 = layout_profiles.content_hash(text)
    assert h1 == h2
    assert len(h1) == 64
