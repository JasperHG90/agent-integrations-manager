"""Tests for agent_files.write_agent_files: base ownership and symlink safety."""

from __future__ import annotations

from pathlib import Path

from aim.core import agent_files, agents_md, layout_profiles
from aim.core.models import Manifest


def _write(project_root: Path, m: Manifest, *, force: bool = False) -> list[str]:
    profile = layout_profiles.BUILTIN_CLAUDE
    return agent_files.write_agent_files(project_root, m, profile, force=force)


def test_fresh_render_records_base_ownership(home: Path, project_root: Path) -> None:
    m = Manifest()
    _write(project_root, m)
    assert m.managed_base_hash is not None
    text = (project_root / "AGENTS.md").read_text()
    assert "<!-- BEGIN aim: header -->" in text


def test_hand_edited_base_survives_forceless_rerender(home: Path, project_root: Path) -> None:
    m = Manifest(symlinks=[])
    _write(project_root, m)
    agents = project_root / "AGENTS.md"
    agents.write_text(agents.read_text() + "\n## Team lore\n\nKeep me.\n")

    warnings = _write(project_root, m)
    assert "Team lore" in agents.read_text()
    assert warnings == []  # preserving user prose is the contract, not drift


def test_force_overwrites_edited_base_with_warning(home: Path, project_root: Path) -> None:
    m = Manifest(symlinks=[])
    _write(project_root, m)
    agents = project_root / "AGENTS.md"
    agents.write_text(agents.read_text() + "\n## Team lore\n")

    warnings = _write(project_root, m, force=True)
    assert "Team lore" not in agents.read_text()
    assert any("overwriting (--force)" in w for w in warnings)
    # aim owns the base again: the next forceless render may swap it freely.
    assert m.managed_base_hash is not None


def test_symlink_equal_to_agents_md_is_refused(home: Path, project_root: Path) -> None:
    """A legacy manifest can carry symlinks=['AGENTS.md']; writing that link
    would brick the project with ELOOP — refuse it with a warning instead."""
    m = Manifest(symlinks=["AGENTS.md", "CLAUDE.md"])
    warnings = _write(project_root, m)
    agents = project_root / "AGENTS.md"
    assert not agents.is_symlink()
    assert agents.read_text()  # readable, not ELOOP
    assert any("refusing to mirror" in w for w in warnings)
    assert (project_root / "CLAUDE.md").is_symlink()  # the valid mirror still lands
    assert "AGENTS.md" not in [Path(f).name for f in m.managed_files[1:]]


def test_nested_agents_md_mirror_resolves(home: Path, project_root: Path) -> None:
    """A profile with agents_md in a subdirectory must still produce a working
    root-level mirror (regression: symlink_to(name) dropped the directory)."""
    profile = layout_profiles.LayoutProfile(name="nested", agents_md="docs/AGENTS.md", symlinks=[])
    m = Manifest(symlinks=["CLAUDE.md"])
    agent_files.write_agent_files(project_root, m, profile)
    mirror = project_root / "CLAUDE.md"
    assert mirror.is_symlink()
    assert mirror.exists()  # not dangling
    assert mirror.resolve() == (project_root / "docs" / "AGENTS.md").resolve()


def test_base_text_strips_all_regions() -> None:
    doc = (
        "prefix\n"
        "<!-- BEGIN aim: header -->\nh\n<!-- END aim: header -->\n"
        "middle\n"
        "<!-- BEGIN aim: rules -->\nr\n<!-- END aim: rules -->\n"
        "suffix\n"
    )
    assert agents_md.base_text(doc) == "prefix\n\nmiddle\n\nsuffix\n"
