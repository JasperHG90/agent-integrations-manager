from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import install, manifest, repos
from tests.fixtures import git_fixtures


def test_update_many_forwards_override_risk(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _src_a, bare_a, _src_b, _bare_b = _two_repo_setup(tmp_path)
    repos.add("a", f"file://{bare_a}")
    install.install(project_root, "a/foo")

    captured: list[bool] = []
    real_update = install.update

    def spy(project_root, qualified_name, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(bool(kwargs.get("override_risk")))
        return real_update(project_root, qualified_name, **kwargs)

    monkeypatch.setattr(install, "update", spy)
    install.update_many(project_root, override_risk=True)
    assert captured and all(captured)


def _two_repo_setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Two repos, two skills each; install all four into a project."""
    src_a = git_fixtures.make_source_repo(
        tmp_path / "src-a",
        files={"skills/foo/SKILL.md": "# foo\n", "skills/bar/SKILL.md": "# bar\n"},
    )
    bare_a = git_fixtures.make_bare_remote(src_a, tmp_path / "bare-a.git")
    src_b = git_fixtures.make_source_repo(
        tmp_path / "src-b",
        files={"skills/baz/SKILL.md": "# baz\n", "skills/qux/SKILL.md": "# qux\n"},
    )
    bare_b = git_fixtures.make_bare_remote(src_b, tmp_path / "bare-b.git")
    return src_a, bare_a, src_b, bare_b


def test_update_many_all_noop_when_at_head(home: Path, project_root: Path, tmp_path: Path) -> None:
    _src_a, bare_a, _src_b, bare_b = _two_repo_setup(tmp_path)
    repos.add("a", f"file://{bare_a}")
    repos.add("b", f"file://{bare_b}")
    for qn in ("a/foo", "a/bar", "b/baz", "b/qux"):
        install.install(project_root, qn)
    outcomes = install.update_many(project_root)
    statuses = [o.status for o in outcomes]
    assert all(s == "updated" for s in statuses)  # no filter passed, --outdated false


def test_update_many_outdated_skips_unchanged(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    src_a, bare_a, _src_b, _bare_b = _two_repo_setup(tmp_path)
    repos.add("a", f"file://{bare_a}")
    install.install(project_root, "a/foo")
    install.install(project_root, "a/bar")

    git_fixtures.add_commit(src_a, {"skills/foo/SKILL.md": "# foo v2\n"}, "foo v2")
    git_fixtures.push_to_bare(src_a, bare_a)
    repos.refresh("a")

    outcomes = install.update_many(project_root, only_outdated=True)
    by_skill = {o.qualified_name: o for o in outcomes}
    assert by_skill["a/foo"].status == "updated"
    assert by_skill["a/bar"].status == "noop"


def test_update_many_repo_filter(home: Path, project_root: Path, tmp_path: Path) -> None:
    _src_a, bare_a, _src_b, bare_b = _two_repo_setup(tmp_path)
    repos.add("a", f"file://{bare_a}")
    repos.add("b", f"file://{bare_b}")
    install.install(project_root, "a/foo")
    install.install(project_root, "b/baz")
    outcomes = install.update_many(project_root, repo_alias="a")
    by_skill = {o.qualified_name: o for o in outcomes}
    assert by_skill["a/foo"].status == "updated"
    assert by_skill["b/baz"].status == "skipped"


def test_update_many_dry_run_does_not_apply(home: Path, project_root: Path, tmp_path: Path) -> None:
    src_a, bare_a, _src_b, _bare_b = _two_repo_setup(tmp_path)
    repos.add("a", f"file://{bare_a}")
    install.install(project_root, "a/foo")
    initial_sha = manifest.load(project_root).skills[0].current.sha

    git_fixtures.add_commit(src_a, {"skills/foo/SKILL.md": "# foo v2\n"}, "foo v2")
    git_fixtures.push_to_bare(src_a, bare_a)
    repos.refresh("a")

    outcomes = install.update_many(project_root, dry_run=True)
    assert any(o.status == "would-update" for o in outcomes)
    # Not applied:
    assert manifest.load(project_root).skills[0].current.sha == initial_sha
