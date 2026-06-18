"""Tests for rule front-matter parsing and transitive composition."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aim.core import init as init_mod
from aim.core import rule_compose, rules
from aim.core import sync as sync_mod
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run


def test_no_front_matter_is_default_order() -> None:
    meta = rule_compose.parse_front_matter("Just a body.\n")
    assert meta.extends == ()
    assert meta.order == rule_compose.DEFAULT_ORDER
    assert meta.body_without_frontmatter == "Just a body.\n"


def test_parses_extends_list_and_order() -> None:
    body = "---\nextends: [a, b, c]\norder: 5\n---\nbody\n"
    meta = rule_compose.parse_front_matter(body)
    assert meta.extends == ("a", "b", "c")
    assert meta.order == 5
    assert meta.body_without_frontmatter == "body\n"


def test_parses_quoted_names() -> None:
    body = "---\nextends: ['quoted-one', \"quoted-two\"]\n---\nbody\n"
    meta = rule_compose.parse_front_matter(body)
    assert meta.extends == ("quoted-one", "quoted-two")


def test_resolve_expands_transitively(home: Path) -> None:
    rules.add("a", "---\nextends: [b]\norder: 50\n---\nbody-a\n")
    rules.add("b", "---\nextends: [c]\norder: 30\n---\nbody-b\n")
    rules.add("c", "---\norder: 10\n---\nbody-c\n")

    resolved = rule_compose.resolve(["a"], lambda n: rules.get(n).body)
    # c before b before a (by order)
    assert resolved == ["c", "b", "a"]


def test_resolve_detects_cycle(home: Path) -> None:
    rules.add("x", "---\nextends: [y]\n---\nx\n")
    rules.add("y", "---\nextends: [x]\n---\ny\n")
    with pytest.raises(rule_compose.RuleCycleError):
        rule_compose.resolve(["x"], lambda n: rules.get(n).body)


def test_init_includes_transitively_extended_rules(home: Path, project_root: Path) -> None:
    from aim.core import layout_profiles

    rules.add("parent", "---\norder: 10\n---\nParent body.\n", is_default=False)
    rules.add(
        "child",
        "---\nextends: [parent]\norder: 20\n---\nChild body.\n",
    )
    # Inline mode renders rule bodies directly into AGENTS.md so we can verify
    # transitive expansion and ordering without reading separate rule files.
    layout_profiles.save_project_profile(
        project_root,
        layout_profiles.LayoutProfile(
            name="inline",
            skills_dir=".claude/skills",
            rules_dir=".claude/rules",
            agents_dir=".claude/agents",
            agents_md="AGENTS.md",
            mcp_json=".mcp.json",
            rules_mode="inline",
        ),
    )
    init_mod.run(
        init_mod.InitOptions(
            project_root=project_root, layout_profile="inline", extra_rules=["child"]
        )
    )
    asyncio.run(lock_run(LockOptions(project_root=project_root)))
    asyncio.run(
        sync_mod.run(sync_mod.SyncOptions(project_root=project_root, layout_profile="inline"))
    )
    text = (project_root / "AGENTS.md").read_text()
    assert "Parent body." in text
    assert "Child body." in text
    # parent should appear before child (lower order):
    assert text.index("Parent body.") < text.index("Child body.")
