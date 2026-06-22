"""Discovery, indexing, and apply for repo-hosted project templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import declarations, manifest, profiles, repo_templates, repos
from aim.core import init as init_mod
from tests.fixtures import git_fixtures

_SVC_TOML = """
name = "svc"
description = "python service template"
instruction_template = "default"

[[skill]]
qualified_name = "src/foo"
"""

_BAD_REPO_TOML = """
name = "needsrepo"
instruction_template = "default"

[[repo]]
alias = "other"
url = "file:///does/not/exist.git"

[[skill]]
qualified_name = "other/foo"
"""


def _repo_with_template(tmp_path: Path, *, extra: dict[str, str] | None = None) -> str:
    files = {"skills/foo/SKILL.md": "# foo\n", "templates/svc.toml": _SVC_TOML}
    files.update(extra or {})
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return f"file://{bare}"


def test_discover_and_list(home: Path, tmp_path: Path) -> None:
    repos.add("src", _repo_with_template(tmp_path))
    rows = repo_templates.list_templates("src")
    assert [r.qualified_name for r in rows] == ["src/svc"]
    assert rows[0].description == "python service template"
    assert "template" in repos.artifact_kinds("src")


def test_load_template_parses_profile(home: Path, tmp_path: Path) -> None:
    repos.add("src", _repo_with_template(tmp_path))
    profile = repo_templates.load_template("src/svc")
    assert isinstance(profile, profiles.Profile)
    assert [s.qualified_name for s in profile.skills] == ["src/foo"]


def test_apply_from_repo_installs_artifacts(home: Path, project_root: Path, tmp_path: Path) -> None:
    repos.add("src", _repo_with_template(tmp_path))
    result = profiles.apply("src/svc", project_root)
    assert result.installed_skills == ["src/foo"]
    assert (project_root / ".claude" / "skills" / "foo" / "SKILL.md").exists()


def test_apply_from_repo_records_provenance_and_locks(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    url = _repo_with_template(tmp_path)
    repos.add("src", url)
    profiles.apply("src/svc", project_root)

    decl = declarations.load(project_root)
    assert decl.template is not None
    assert decl.template.qualified_name == "src/svc"
    assert decl.template.url == url
    assert decl.template.members == ["src/foo"]
    assert decl.template.ref == repo_templates.index_row("src/svc").indexed_at_sha

    m = manifest.load(project_root)
    assert m.template_qualified_name == "src/svc"
    assert m.template_repo == url
    assert m.template_ref == decl.template.ref
    assert m.template_hash is not None


def test_apply_from_repo_unreachable_source_repo_raises(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import git

    url = _repo_with_template(tmp_path, extra={"templates/needsrepo.toml": _BAD_REPO_TOML})
    repos.add("src", url)
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    # The template's [[repo]] url is bogus, so auto-registration fails to clone it.
    with pytest.raises(git.GitError):
        profiles.apply("src/needsrepo", project_root)


def test_template_only_repo_registers_without_allow_empty(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"templates/svc.toml": _SVC_TOML}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("tmpl", f"file://{bare}")
    assert [r.qualified_name for r in repo_templates.list_templates("tmpl")] == ["tmpl/svc"]


def test_unparseable_template_is_skipped(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n",
            "templates/broken.toml": "name = 123\n",  # name must be a string
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("src", f"file://{bare}")
    assert repo_templates.list_templates("src") == []
