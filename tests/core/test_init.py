from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import declarations as declarations_mod
from aim.core import init as init_mod
from aim.core import rules


def test_first_init_creates_aim_toml(home: Path, project_root: Path) -> None:
    rules.add("focus", "Focus on simplicity.", is_default=True)
    result = init_mod.run(init_mod.InitOptions(project_root=project_root))
    assert result.re_init is False
    assert result.declarations_path.exists()
    decl = declarations_mod.load(project_root)
    assert decl.rules == ["focus"]
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
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("CLAUDE.md", "GEMINI.md")))
    init_mod.run(init_mod.InitOptions(project_root=project_root, symlinks=("OPENCODE.md",), clear_symlinks=True))
    decl = declarations_mod.load(project_root)
    assert decl.symlinks == ["OPENCODE.md"]


def test_re_init_updates_rules_in_declarations(home: Path, project_root: Path) -> None:
    rules.add("first", "First rule.", is_default=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = declarations_mod.load(project_root)
    assert decl.rules == ["first"]

    rules.add("second", "Second rule.", is_default=True)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    decl = declarations_mod.load(project_root)
    assert "first" in decl.rules
    assert "second" in decl.rules


def test_init_with_no_default_rules(home: Path, project_root: Path) -> None:
    rules.add("would-be-default", "body", is_default=True)
    result = init_mod.run(init_mod.InitOptions(project_root=project_root, seed_default_rules=False))
    assert result.applied_rules == []
    decl = declarations_mod.load(project_root)
    assert decl.rules == []


def test_init_seeds_rule_from_file(home: Path, project_root: Path) -> None:
    rule_file = home / "my-rule.md"
    rule_file.write_text("# My rule\n\nAlways add tests.\n")
    result = init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            extra_rule_files={"my-rule": rule_file},
        )
    )
    assert "my-rule" in result.applied_rules
    decl = declarations_mod.load(project_root)
    assert "my-rule" in decl.rules
    assert rules.get("my-rule").body == "# My rule\n\nAlways add tests.\n"


def test_init_rejects_invalid_rule_file_name(home: Path, project_root: Path) -> None:
    rule_file = home / "bad rule.md"
    rule_file.write_text("# Bad\n")
    with pytest.raises(rules.RuleNameError):
        init_mod.run(
            init_mod.InitOptions(
                project_root=project_root,
                extra_rule_files={"bad rule": rule_file},
            )
        )


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


def test_init_records_agent_dialect(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root, agent_dialect="claude"))
    decl = declarations_mod.load(project_root)
    assert decl.agent_dialect == "claude"
