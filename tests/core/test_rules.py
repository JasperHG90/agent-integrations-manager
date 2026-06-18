from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import content_guard, init, manifest, repos, rule_install
from tests.fixtures import git_fixtures


def _make_project_and_repo(
    tmp_path: Path, project_root: Path, body: str = "Be concise.\n"
) -> tuple[Path, str]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "rules/be-concise.md": body,
            "README.md": "x\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    init.run(init.InitOptions(project_root=project_root))
    repos.add("anth", f"file://{bare}")
    return bare, "anth/be-concise"


def test_install_writes_rule_file(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    installed = rule_install.install(project_root, qn)
    assert installed.qualified_name == qn
    target = project_root / ".claude" / "rules" / "be-concise.md"
    assert target.exists()
    assert "Be concise." in target.read_text()

    m = manifest.load(project_root)
    assert len(m.rules) == 1
    assert m.rules[0].source_path == "rules/be-concise.md"
    assert m.rules[0].content_hash is not None


def test_install_mirrors_to_declarations(home: Path, tmp_path: Path, project_root: Path) -> None:
    from aim.core import declarations

    _, qn = _make_project_and_repo(tmp_path, project_root)
    rule_install.install(project_root, qn)
    decl = declarations.load(project_root)
    assert [r.qualified_name for r in decl.rules] == [qn]
    assert decl.repos["anth"]


def test_update_refreshes_rule(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    rule_install.install(project_root, qn)

    working = tmp_path / "src"
    git_fixtures.add_commit(working, {"rules/be-concise.md": "Updated rule.\n"}, "update rule")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    result = rule_install.update(project_root, qn)
    assert result.current.sha != result.history[0].sha
    assert "Updated rule." in (project_root / ".claude" / "rules" / "be-concise.md").read_text()


def test_update_skips_when_unchanged(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    first = rule_install.install(project_root, qn)
    second = rule_install.update(project_root, qn)
    assert first.current.sha == second.current.sha
    assert second.history == []


def test_update_detects_local_edits(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    rule_install.install(project_root, qn)

    working = tmp_path / "src"
    git_fixtures.add_commit(working, {"rules/be-concise.md": "Upstream edit.\n"}, "upstream edit")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    target = project_root / ".claude" / "rules" / "be-concise.md"
    target.write_text("tampered")
    with pytest.raises(rule_install.RuleLocalEditsError):
        rule_install.update(project_root, qn)
    rule_install.update(project_root, qn, force=True)


def test_remove_deletes_file_and_manifest_entry(
    home: Path, tmp_path: Path, project_root: Path
) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    rule_install.install(project_root, qn)
    rule_install.delete(project_root, qn)
    assert not (project_root / ".claude" / "rules" / "be-concise.md").exists()
    assert manifest.load(project_root).rules == []


def test_rollback_restores_previous_version(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    rule_install.install(project_root, qn)

    working = tmp_path / "src"
    git_fixtures.add_commit(working, {"rules/be-concise.md": "V2 rule.\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    rule_install.update(project_root, qn)
    assert "V2 rule." in (project_root / ".claude" / "rules" / "be-concise.md").read_text()

    rule_install.rollback(project_root, qn)
    assert "Be concise." in (project_root / ".claude" / "rules" / "be-concise.md").read_text()


def test_update_many_only_outdated(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root)
    rule_install.install(project_root, qn)
    outcomes = rule_install.update_many(project_root, only_outdated=True)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "noop"


def test_install_uses_tag(home: Path, tmp_path: Path, project_root: Path) -> None:
    bare, qn = _make_project_and_repo(tmp_path, project_root)
    working = tmp_path / "src"
    git_fixtures.add_tag(working, "v1.0.0")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("anth")

    installed = rule_install.install(project_root, qn)
    assert installed.current.tag == "v1.0.0"


def test_install_rejects_hidden_unicode(home: Path, tmp_path: Path, project_root: Path) -> None:
    _, qn = _make_project_and_repo(tmp_path, project_root, body="Be concise.\n\nhidden​\n")
    with pytest.raises(content_guard.HiddenUnicodeError):
        rule_install.install(project_root, qn)
    assert not (project_root / ".claude" / "rules" / "be-concise.md").exists()
