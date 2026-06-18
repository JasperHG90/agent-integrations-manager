from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import repos, skills
from tests.fixtures import git_fixtures


def _build_repo_with(
    tmp_path: Path,
    files: dict[str, str],
) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def test_discover_finds_canonical_skill(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/code-review/SKILL.md": "# Code Review\n\nReview a PR carefully.\n",
            "README.md": "x\n",
        },
    )
    repos.add("anth", f"file://{bare}")
    rows = skills.list_skills()
    assert len(rows) == 1
    assert rows[0].qualified_name == "anth/code-review"
    assert rows[0].source_path == "skills/code-review"
    assert rows[0].title == "Code Review"
    assert rows[0].description and "Review a PR" in rows[0].description


def test_discover_supports_claude_path(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            ".claude/skills/foo/SKILL.md": "# Foo\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert [r.qualified_name for r in rows] == ["a/foo"]
    assert rows[0].source_path == ".claude/skills/foo"


def test_discover_supports_root_path(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "rootskill/SKILL.md": "# Root Skill\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert [r.qualified_name for r in rows] == ["a/rootskill"]
    assert rows[0].source_path == "rootskill"


def test_discover_supports_bare_root_skill_md(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "SKILL.md": "# Bare Root Skill\n\nA skill at repo root.\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert [r.qualified_name for r in rows] == ["a/a"]
    assert rows[0].source_path == ""
    assert rows[0].title == "Bare Root Skill"
    assert rows[0].description == "A skill at repo root."


def test_frontmatter_name_and_description(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/algorithmic-art/SKILL.md": (
                "---\n"
                "name: algorithmic-art\n"
                "description: Generate algorithmic art with structured prompts.\n"
                "---\n\n"
                "## Safety\n\n"
                "Never execute untrusted code.\n\n"
                "## Usage\n\n"
                "Use `/algorithmic-art` to start.\n"
            ),
            "README.md": "x\n",
        },
    )
    repos.add("anth", f"file://{bare}")
    rows = skills.list_skills()
    assert len(rows) == 1
    assert rows[0].qualified_name == "anth/algorithmic-art"
    assert rows[0].title == "algorithmic-art"
    assert rows[0].description == "Generate algorithmic art with structured prompts."
    assert rows[0].prereqs == ""
    assert rows[0].provides == ""


def test_frontmatter_name_with_body_description(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/custom/SKILL.md": (
                "---\n"
                "name: custom-name\n"
                "---\n\n"
                "# Body Heading\n\n"
                "This description comes from the body.\n\n"
                "More body text.\n"
            ),
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert len(rows) == 1
    assert rows[0].qualified_name == "a/custom"
    assert rows[0].title == "custom-name"
    assert rows[0].description == "This description comes from the body."


def test_frontmatter_prereqs_and_provides(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/full/SKILL.md": (
                "---\n"
                "name: full-meta\n"
                "description: Does everything.\n"
                "prereqs: [other/base, other/util]\n"
                "provides: code-review\n"
                "---\n\n"
                "# Full Meta\n\n"
                "Body here.\n"
            ),
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert len(rows) == 1
    assert rows[0].qualified_name == "a/full"
    assert rows[0].title == "full-meta"
    assert rows[0].description == "Does everything."
    assert rows[0].prereqs == "other/base,other/util"
    assert rows[0].provides == "code-review"


def test_discover_supports_nested_skills_dir(home: Path, tmp_path: Path) -> None:
    """Repos like google/skills group skills under sub-categories: skills/cloud/<name>/SKILL.md."""
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/cloud/bigquery-basics/SKILL.md": "# BigQuery\n\nBigQuery primer.\n",
            "skills/cloud/cloud-run-basics/SKILL.md": "# Cloud Run\n",
            "skills/data/dataform/SKILL.md": "# Dataform\n",
            "README.md": "x\n",
        },
    )
    repos.add("google", f"file://{bare}")
    rows = skills.list_skills()
    names = {r.qualified_name for r in rows}
    assert names == {
        "google/bigquery-basics",
        "google/cloud-run-basics",
        "google/dataform",
    }
    paths = {r.source_path for r in rows}
    assert paths == {
        "skills/cloud/bigquery-basics",
        "skills/cloud/cloud-run-basics",
        "skills/data/dataform",
    }


def test_discover_supports_plugin_skills_dir(home: Path, tmp_path: Path) -> None:
    """Repos like wshobson/agents group skills under plugins/<cat>/skills/<name>."""
    _, bare = _build_repo_with(
        tmp_path,
        {
            "plugins/business-analytics/skills/data-storytelling/SKILL.md": (
                "# Data Storytelling\n\nTell stories with data.\n"
            ),
            "plugins/python-development/skills/async/SKILL.md": "# Async\n",
            "README.md": "x\n",
        },
    )
    repos.add("wshobson", f"file://{bare}")
    rows = skills.list_skills()
    names = {r.qualified_name for r in rows}
    assert names == {"wshobson/data-storytelling", "wshobson/async"}
    paths = {r.qualified_name: r.source_path for r in rows}
    assert (
        paths["wshobson/data-storytelling"] == "plugins/business-analytics/skills/data-storytelling"
    )
    assert paths["wshobson/async"] == "plugins/python-development/skills/async"


def test_precedence_skills_dir_wins(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/dup/SKILL.md": "# canonical\n",
            ".claude/skills/dup/SKILL.md": "# shadow\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert [r.qualified_name for r in rows] == ["a/dup"]
    assert rows[0].source_path == "skills/dup"
    # Re-run discover directly to inspect shadowed list.
    d = skills.discover("a")
    assert any(s.source_path == ".claude/skills/dup" for s in d.shadowed)


def test_precedence_canonical_wins_over_plugin_skill(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/dup/SKILL.md": "# canonical\n",
            "plugins/cat/skills/dup/SKILL.md": "# plugin shadow\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    assert [r.qualified_name for r in rows] == ["a/dup"]
    assert rows[0].source_path == "skills/dup"
    d = skills.discover("a")
    assert any(s.source_path == "plugins/cat/skills/dup" for s in d.shadowed)


def test_empty_repo_registration_fails_and_rolls_back(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(tmp_path, {"README.md": "no skills here\n"})
    with pytest.raises(repos.RepoHasNoSkillsError):
        repos.add("empty", f"file://{bare}")
    # Roll-back: nothing left in DB or on disk.
    with pytest.raises(repos.RepoNotFoundError):
        repos.get("empty")
    assert not repos.clone_dir("empty").exists()


def test_allow_empty_registers_anyway(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(tmp_path, {"README.md": "x\n"})
    repos.add("ok", f"file://{bare}", allow_empty=True)
    assert repos.get("ok").alias == "ok"
    assert skills.list_skills("ok") == []


def test_discover_rejects_invalid_skill_names(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/.hidden/SKILL.md": "# Hidden\n",
            "skills/foo-bar/SKILL.md": "# Valid\n",
            "README.md": "x\n",
        },
    )
    repos.add("a", f"file://{bare}")
    rows = skills.list_skills()
    # `.hidden` is invalid (starts with `.`), so only the valid skill is indexed.
    assert [r.qualified_name for r in rows] == ["a/foo-bar"]


def test_search_matches_qualified_name(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {
            "skills/review/SKILL.md": "# Review\n",
            "skills/format/SKILL.md": "# Format\n",
        },
    )
    repos.add("a", f"file://{bare}")
    hits = skills.search("review")
    assert [r.qualified_name for r in hits] == ["a/review"]


def test_refresh_reindexes_when_sha_changes(home: Path, bare_remote: tuple[Path, Path]) -> None:
    working, bare = bare_remote
    repos.add("anth", f"file://{bare}")
    initial = [r.qualified_name for r in skills.list_skills()]
    assert initial == ["anth/foo"]

    git_fixtures.add_commit(
        working,
        {"skills/bar/SKILL.md": "# bar\n"},
        "add bar skill",
    )
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")
    after = sorted(r.qualified_name for r in skills.list_skills())
    assert after == ["anth/bar", "anth/foo"]


def test_remove_clears_skill_index(home: Path, bare_remote: tuple[Path, Path]) -> None:
    _, bare = bare_remote
    repos.add("anth", f"file://{bare}")
    assert skills.list_skills("anth")
    repos.remove("anth")
    assert skills.list_skills("anth") == []


def test_read_skill_content(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {"skills/b/SKILL.md": "---\nname: B\n---\n# B body\n"},
    )
    repos.add("a", f"file://{bare}")
    content = skills.read_skill_content("a/b")
    assert "# B body" in content


def test_read_skill_content_bare_root(home: Path, tmp_path: Path) -> None:
    _, bare = _build_repo_with(
        tmp_path,
        {"SKILL.md": "# Bare Root Skill\n", "README.md": "x\n"},
    )
    repos.add("a", f"file://{bare}")
    content = skills.read_skill_content("a/a")
    assert "# Bare Root Skill" in content


def test_read_skill_content_missing_raises(home: Path, tmp_path: Path) -> None:
    with pytest.raises(skills.SkillNotIndexedError):
        skills.read_skill_content("a/missing")
