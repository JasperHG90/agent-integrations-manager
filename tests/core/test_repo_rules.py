from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import repo_rules, repos
from tests.fixtures import git_fixtures


def _build_repo_with(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def test_discover_finds_canonical_rule(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/be-concise.md": "Be concise.\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/be-concise"]
    assert rows[0].rule_md_path == "rules/be-concise.md"


def test_discover_finds_claude_rules_dir(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".claude/rules/be-concise.md": "Be concise.\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/be-concise"]
    assert rows[0].rule_md_path == ".claude/rules/be-concise.md"


def test_discover_ignores_root_level_and_arbitrary_paths(home: Path, tmp_path: Path) -> None:
    # Only `rules/` and `.claude/rules/` are scanned. Root-level and arbitrary
    # `.md` files are not surfaced as installable rules.
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/keep.md": "Keep.\n",
            "be-concise.md": "ignored\n",
            "docs/conventions/style.md": "ignored\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/keep"]


def test_discover_filters_documentation_names(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "README.md": "x\n",
            "license.md": "MIT\n",
            "CHANGELOG.md": "v1\n",
            "docs/code_of_conduct.md": "Be nice.\n",
            "rules/be-concise.md": "Be concise.\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/be-concise"]


def test_precedence_rules_dir_wins(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/dup.md": "canonical\n",
            ".claude/rules/dup.md": "claude shadow\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/dup"]
    assert rows[0].rule_md_path == "rules/dup.md"
    d = repo_rules.discover("a")
    assert any(r.rule_md_path == ".claude/rules/dup.md" for r in d.shadowed)


def test_discover_finds_apm_instructions(home: Path, tmp_path: Path) -> None:
    # APM packages keep rules as `<name>.instructions.md` under
    # `.apm/instructions/` or `.github/instructions/`; the rule name drops the
    # `.instructions.md` suffix.
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".apm/instructions/python-style.instructions.md": "Use type hints.\n",
            ".github/instructions/testing.instructions.md": "Write tests.\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [(r.qualified_name, r.rule_md_path) for r in rows] == [
        ("a/python-style", ".apm/instructions/python-style.instructions.md"),
        ("a/testing", ".github/instructions/testing.instructions.md"),
    ]


def test_discover_apm_instructions_matches_apm_patterns_only(home: Path, tmp_path: Path) -> None:
    # APM scans `instructions/*.instructions.md` only: no plain `.md`, no
    # subdirectories, and no `.instructions.md` outside the two directories.
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".apm/instructions/keep.instructions.md": "keep\n",
            ".apm/instructions/plain.md": "ignored\n",
            ".apm/instructions/sub/nested.instructions.md": "ignored\n",
            ".github/instructions/notes.md": "ignored\n",
            "docs/stray.instructions.md": "ignored\n",
            ".github/copilot-instructions.md": "ignored\n",
        },
    )
    repos.add("a", f"file://{bare}")
    d = repo_rules.discover("a")
    assert [r.rule_md_path for r in d.indexed] == [".apm/instructions/keep.instructions.md"]
    assert d.shadowed == []


def test_precedence_apm_dir_wins_over_github(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".apm/instructions/dup.instructions.md": "apm\n",
            ".github/instructions/dup.instructions.md": "github\n",
            "rules/other.md": "x\n",
            ".apm/instructions/other.instructions.md": "apm\n",
        },
    )
    repos.add("a", f"file://{bare}")
    d = repo_rules.discover("a")
    assert {r.name: r.rule_md_path for r in d.indexed} == {
        "dup": ".apm/instructions/dup.instructions.md",
        "other": "rules/other.md",
    }
    assert sorted(r.rule_md_path for r in d.shadowed) == [
        ".apm/instructions/other.instructions.md",
        ".github/instructions/dup.instructions.md",
    ]


def test_precedence_apm_dir_wins_over_claude_rules(home: Path, tmp_path: Path) -> None:
    # Same depth: the APM dirs rank with `rules/`, ahead of `.claude/rules/`.
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".apm/instructions/dup.instructions.md": "apm\n",
            ".claude/rules/dup.md": "claude\n",
        },
    )
    repos.add("a", f"file://{bare}")
    d = repo_rules.discover("a")
    assert [r.rule_md_path for r in d.indexed] == [".apm/instructions/dup.instructions.md"]
    assert [r.rule_md_path for r in d.shadowed] == [".claude/rules/dup.md"]


def test_arbitrary_path_not_even_shadowed(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/dup.md": "canonical\n",
            "docs/dup.md": "ignored\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/dup"]
    assert rows[0].rule_md_path == "rules/dup.md"
    d = repo_rules.discover("a")
    # Arbitrary paths are no longer discovered at all — not even as shadowed.
    assert all(r.rule_md_path != "docs/dup.md" for r in d.shadowed)


def test_discover_rejects_unsafe_paths(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/ok.md": "ok\n",
            "../escape.md": "escape\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [r.qualified_name for r in rows] == ["a/ok"]


def test_read_rule_content(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {"rules/be-concise.md": "Be concise.\n"},
    )
    repos.add("a", f"file://{bare}")
    assert repo_rules.read_rule_content("a/be-concise") == "Be concise.\n"


def test_read_rule_content_missing_raises(home: Path, tmp_path: Path) -> None:
    with pytest.raises(repo_rules.RuleNotIndexedError):
        repo_rules.read_rule_content("a/missing")


# ---------------------------------------------------------------------------
# provenance (origin) filters
# ---------------------------------------------------------------------------


def test_plugin_owned_rule_indexed_shown_by_default(home: Path, tmp_path: Path) -> None:
    import json

    marketplace = {
        "name": "demo-market",
        "plugins": [{"name": "helper", "source": "./helper", "version": "1.0.0"}],
    }
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "helper/.claude-plugin/plugin.json": json.dumps({"name": "helper"}),
            "helper/rules/style.md": "Style rule.\n",
            "rules/be-concise.md": "Be concise.\n",
        },
    )
    repos.add("pm", f"file://{bare}")
    from aim.core import origins

    rows = repo_rules.list_rules()  # shown by default
    assert [r.qualified_name for r in rows] == ["pm/be-concise", "pm/style"]
    by_name = {r.qualified_name: r for r in rows}
    assert by_name["pm/style"].origin == origins.ORIGIN_PLUGIN
    assert by_name["pm/style"].owning_plugin == "helper"
    # opt-out hides the plugin-owned rule, leaving the standalone one
    standalone = repo_rules.list_rules(include_plugin_owned=False)
    assert [r.qualified_name for r in standalone] == ["pm/be-concise"]


def test_dot_claude_rule_excludable(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rules/be-concise.md": "Be concise.\n",
            ".claude/rules/dotted.md": "Dotted rule.\n",
        },
    )
    repos.add("a", f"file://{bare}")
    assert [r.qualified_name for r in repo_rules.list_rules()] == ["a/be-concise", "a/dotted"]
    only_canon = repo_rules.list_rules(include_dot_claude=False)
    assert [r.qualified_name for r in only_canon] == ["a/be-concise"]


def test_plugin_owned_apm_instruction_indexed(home: Path, tmp_path: Path) -> None:
    import json

    from aim.core import origins

    marketplace = {
        "name": "demo-market",
        "plugins": [{"name": "helper", "source": "./helper", "version": "1.0.0"}],
    }
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "helper/.claude-plugin/plugin.json": json.dumps({"name": "helper"}),
            "helper/.apm/instructions/style.instructions.md": "Style rule.\n",
        },
    )
    repos.add("pm", f"file://{bare}")
    rows = repo_rules.list_rules()
    assert [(r.qualified_name, r.rule_md_path) for r in rows] == [
        ("pm/style", "helper/.apm/instructions/style.instructions.md")
    ]
    assert rows[0].origin == origins.ORIGIN_PLUGIN
    assert rows[0].owning_plugin == "helper"
