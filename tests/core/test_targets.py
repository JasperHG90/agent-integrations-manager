"""Target (plugin-kind TOML) discovery from registered repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import repos, targets
from tests.fixtures import git_fixtures

_OPENCODE_TARGET = """
name = "opencode"
[manifest]
file = "package.json"
[register]
vendor_into = ".opencode/plugins/{name}"
"""

_ESCAPING_TARGET = """
name = "escaper"
[manifest]
file = "package.json"
[register]
vendor_into = "../../escape/{name}"
"""


def _build(tmp_path: Path, files: dict[str, str]) -> str:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return f"file://{bare}"


def test_targets_only_repo_registers_and_indexes(home: Path, tmp_path: Path) -> None:
    url = _build(tmp_path, {"targets/opencode.toml": _OPENCODE_TARGET, "README.md": "x\n"})
    repos.add("a", url)  # must not raise RepoHasNoArtifactsError
    assert "target" in repos.artifact_kinds("a")
    rows = targets.list_targets("a")
    assert [(r.target_name, r.qualified_name) for r in rows] == [("opencode", "a/opencode")]
    assert rows[0].target_toml_path == "targets/opencode.toml"
    assert 'name = "opencode"' in targets.read_target_content("a/opencode")


def test_invalid_target_spec_skipped_with_warning(home: Path, tmp_path: Path) -> None:
    targets.take_index_warnings()  # drain any prior warnings
    # The escaping vendor_into is rejected at spec parse, so the repo has no usable
    # target and (with nothing else) is rejected.
    url = _build(tmp_path, {"targets/escaper.toml": _ESCAPING_TARGET})
    with pytest.raises(repos.RepoHasNoArtifactsError):
        repos.add("a", url)
    warnings = targets.take_index_warnings()
    assert any("escaper.toml" in w and "invalid target spec" in w for w in warnings)


def test_non_targets_dir_toml_not_discovered(home: Path, tmp_path: Path) -> None:
    # A valid spec OUTSIDE targets/ must not be discovered (only canonical location).
    url = _build(tmp_path, {"config/opencode.toml": _OPENCODE_TARGET, "rules/a.md": "a\n"})
    repos.add("a", url)
    assert targets.list_targets("a") == []


def test_reindex_picks_up_added_target(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"rules/a.md": "a\n", "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    assert targets.list_targets("a") == []

    git_fixtures.add_commit(working, {"targets/opencode.toml": _OPENCODE_TARGET}, "add target")
    git_fixtures.push_to_bare(working, bare)
    repos.reindex("a")
    assert [r.target_name for r in targets.list_targets("a")] == ["opencode"]
