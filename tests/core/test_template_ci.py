"""CI primitives for repo-hosted templates: check, diff, update."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aim.core import declarations, manifest, profiles, repos
from tests.fixtures import git_fixtures

_V1 = """
name = "svc"
description = "v1"
instruction_template = "default"

[[rule]]
qualified_name = "src/be-concise"

[[skill]]
qualified_name = "src/foo"
"""

# v2 drops the rule and adds skill bar.
_V2 = """
name = "svc"
description = "v2"
instruction_template = "default"

[[skill]]
qualified_name = "src/foo"

[[skill]]
qualified_name = "src/bar"
"""


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """Build a template repo (v1) and return (working_tree, url)."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n",
            "rules/be-concise.md": "Be concise.\n",
            "templates/svc.toml": _V1,
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, f"file://{bare}"


def _advance(working: Path, tmp_path: Path, files: dict[str, str]) -> None:
    """Commit `files` to the working tree and push to its bare remote."""
    git_fixtures.add_commit(working, files, "advance")
    subprocess.run(
        ["git", "-C", str(working), "push", "--quiet", str(tmp_path / "bare.git"), "main"],
        check=True,
        capture_output=True,
    )


def test_check_up_to_date_after_apply(home: Path, project_root: Path, tmp_path: Path) -> None:
    _working, url = _make_repo(tmp_path)
    repos.add("src", url)
    profiles.apply("src/svc", project_root)
    result = profiles.check(project_root)
    assert result.has_template
    assert result.up_to_date


def test_check_reports_template_drift(home: Path, project_root: Path, tmp_path: Path) -> None:
    working, url = _make_repo(tmp_path)
    repos.add("src", url)
    profiles.apply("src/svc", project_root)

    _advance(working, tmp_path, {"templates/svc.toml": _V2, "skills/bar/SKILL.md": "# bar\n"})
    repos.refresh("src")

    result = profiles.check(project_root)
    assert result.drift is True
    assert result.upstream_hash != result.locked_hash


def test_check_no_template_exits_clean(home: Path, project_root: Path, tmp_path: Path) -> None:
    from aim.core import init as init_mod

    init_mod.run(init_mod.InitOptions(project_root=project_root))
    result = profiles.check(project_root)
    assert result.has_template is False
    assert result.up_to_date


def test_update_adds_and_removes_members(home: Path, project_root: Path, tmp_path: Path) -> None:
    working, url = _make_repo(tmp_path)
    repos.add("src", url)
    profiles.apply("src/svc", project_root)

    m = manifest.load(project_root)
    assert sorted(s.qualified_name for s in m.skills) == ["src/foo"]
    assert [r.qualified_name for r in m.rules] == ["src/be-concise"]

    _advance(working, tmp_path, {"templates/svc.toml": _V2, "skills/bar/SKILL.md": "# bar\n"})
    repos.refresh("src")

    result = profiles.update_from_template(project_root)
    assert result.removed == ["src/be-concise"]

    m2 = manifest.load(project_root)
    assert sorted(s.qualified_name for s in m2.skills) == ["src/bar", "src/foo"]
    assert m2.rules == []

    decl = declarations.load(project_root)
    assert decl.template is not None
    assert sorted(decl.template.members) == ["src/bar", "src/foo"]
    # After converging, the project is up to date again.
    assert profiles.check(project_root).up_to_date


def test_update_preserves_user_added_artifact(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import install

    working, url = _make_repo(tmp_path)
    repos.add("src", url)
    profiles.apply("src/svc", project_root)

    # User adds an extra skill not owned by the template.
    _advance(working, tmp_path, {"skills/extra/SKILL.md": "# extra\n"})
    repos.refresh("src")
    install.install(project_root, "src/extra")

    # Template advances (drops the rule, adds bar) and we converge.
    _advance(working, tmp_path, {"templates/svc.toml": _V2, "skills/bar/SKILL.md": "# bar\n"})
    repos.refresh("src")
    profiles.update_from_template(project_root)

    m = manifest.load(project_root)
    # The user's extra skill survives the update.
    assert "src/extra" in [s.qualified_name for s in m.skills]


def test_diff_previews_added_and_removed(home: Path, project_root: Path, tmp_path: Path) -> None:
    working, url = _make_repo(tmp_path)
    repos.add("src", url)
    profiles.apply("src/svc", project_root)

    _advance(working, tmp_path, {"templates/svc.toml": _V2, "skills/bar/SKILL.md": "# bar\n"})
    repos.refresh("src")

    d = profiles.diff(project_root)
    assert d.added == ["src/bar"]
    assert d.removed == ["src/be-concise"]


def test_bump_advances_template_artifact_sha(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import init as init_mod
    from aim.core import install

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo v1\n"}
    )
    git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("src", f"file://{tmp_path / 'bare.git'}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "src/foo")

    profiles.save(profiles.from_project("svc", project_root))
    before = profiles.load("svc").skills[0].sha
    assert before is not None

    _advance(working, tmp_path, {"skills/foo/SKILL.md": "# foo v2\n"})
    repos.refresh("src")

    changes = profiles.bump("svc")
    assert [c.qualified_name for c in changes] == ["src/foo"]
    assert changes[0].old_sha == before
    after = profiles.load("svc").skills[0].sha
    assert after == changes[0].new_sha and after != before


def test_bump_single_artifact_and_unknown_raises(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    from aim.core import init as init_mod
    from aim.core import install

    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo v1\n"}
    )
    git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("src", f"file://{tmp_path / 'bare.git'}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "src/foo")
    profiles.save(profiles.from_project("svc", project_root))

    _advance(working, tmp_path, {"skills/foo/SKILL.md": "# foo v2\n"})
    repos.refresh("src")

    changes = profiles.bump("svc", only="src/foo")
    assert [c.qualified_name for c in changes] == ["src/foo"]

    import pytest

    with pytest.raises(profiles.TemplateArtifactNotFoundError):
        profiles.bump("svc", only="src/nope")
