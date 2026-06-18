from __future__ import annotations

from pathlib import Path

from aim.core import declarations as declarations_mod
from aim.core import init as init_mod
from aim.core.models import DeclaredRule


def test_first_init_creates_aim_toml(home: Path, project_root: Path) -> None:
    result = init_mod.run(init_mod.InitOptions(project_root=project_root))
    assert result.re_init is False
    assert result.declarations_path.exists()
    decl = declarations_mod.load(project_root)
    assert decl.rules == []
    assert decl.instruction_template == "default"
    assert "AGENTS.md" not in result.declarations_path.read_text()


def test_first_init_inherits_profile_symlinks_by_default(home: Path, project_root: Path) -> None:
    """Default layout profile (claude) declares its symlinks in aim.toml."""
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = declarations_mod.load(project_root)
    assert decl.symlinks == ["CLAUDE.md"]
    # init writes declarations only; sync creates the actual symlink files.
    assert not (project_root / "CLAUDE.md").exists()
    assert not (project_root / "GEMINI.md").exists()


def test_first_init_records_symlinks(home: Path, project_root: Path) -> None:
    init_mod.run(
        init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md", "GEMINI.md"))
    )
    decl = declarations_mod.load(project_root)
    assert decl.symlinks == ["CLAUDE.md", "GEMINI.md"]


def test_re_init_preserves_existing_symlinks(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md",)))
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("GEMINI.md",)))
    decl = declarations_mod.load(project_root)
    assert "CLAUDE.md" in decl.symlinks
    assert "GEMINI.md" in decl.symlinks


def test_re_init_clear_symlinks_replaces_them(home: Path, project_root: Path) -> None:
    init_mod.run(
        init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md", "GEMINI.md"))
    )
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root, symlinks=("OPENCODE.md",), clear_symlinks=True
        )
    )
    decl = declarations_mod.load(project_root)
    assert decl.symlinks == ["OPENCODE.md"]


def test_re_init_preserves_repo_sourced_rules(home: Path, project_root: Path) -> None:
    # Rules are repo-sourced and added via `aim rule add`; init must preserve
    # any rule declarations already present on re-init.
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = declarations_mod.load(project_root)
    decl.rules = [
        DeclaredRule(qualified_name="anth/first", repo_alias="anth", source_path="rules/first.md")
    ]
    decl.repos["anth"] = "file:///tmp/anth"
    declarations_mod.save(project_root, decl)

    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = declarations_mod.load(project_root)
    assert [r.qualified_name for r in decl.rules] == ["anth/first"]


def test_init_does_not_seed_rules(home: Path, project_root: Path) -> None:
    result = init_mod.run(init_mod.InitOptions(project_root=project_root))
    assert result.applied_rules == []
    decl = declarations_mod.load(project_root)
    assert decl.rules == []


def test_init_records_layout_profile(home: Path, project_root: Path) -> None:
    from aim.core import layout_profiles

    layout_profiles.save_project_profile(
        project_root,
        layout_profiles.LayoutProfile(
            name="custom",
            skills_dir=".aim/skills",
            rules_dir=".aim/rules",
            agents_dir=".aim/agents",
            mcp_json=".mcp.json",
        ),
    )
    init_mod.run(init_mod.InitOptions(project_root=project_root, layout_profile="custom"))
    decl = declarations_mod.load(project_root)
    assert decl.layout_profile == "custom"


def test_init_records_layout_profile_default(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = declarations_mod.load(project_root)
    assert decl.layout_profile is None
