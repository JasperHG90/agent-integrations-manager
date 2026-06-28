"""Install / update / rollback / delete of plugin targets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aim.core import declarations, lock, manifest, plugins, repos, sync, target_install
from aim.core import init as init_mod
from tests.fixtures import git_fixtures

_TARGET_V1 = """
name = "opencode"
[manifest]
file = "package.json"
[register]
vendor_into = ".opencode/plugins/{name}"
"""

_TARGET_V2 = """
name = "opencode"
[manifest]
file = "package.json"
name = "name"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def _repo(tmp_path: Path, body: str = _TARGET_V1) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"targets/opencode.toml": body, "README.md": "x\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def test_install_vendors_locks_and_declares(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _repo(tmp_path)
    repos.add("a", f"file://{bare}")

    installed = target_install.install(project_root, "a/opencode")

    vendored = project_root / ".aim" / "targets" / "opencode.toml"
    assert vendored.exists()
    assert 'name = "opencode"' in vendored.read_text()
    assert installed.qualified_name == "a/opencode"
    assert installed.content_hash

    m = manifest.load(project_root)
    assert [t.qualified_name for t in m.targets] == ["a/opencode"]

    decl = declarations.load(project_root)
    assert [t.qualified_name for t in decl.targets] == ["a/opencode"]


def test_update_then_rollback(home: Path, project_root: Path, tmp_path: Path) -> None:
    working, bare = _repo(tmp_path)
    repos.add("a", f"file://{bare}")
    target_install.install(project_root, "a/opencode")

    git_fixtures.add_commit(working, {"targets/opencode.toml": _TARGET_V2}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.reindex("a")

    updated = target_install.update(project_root, "a/opencode")
    vendored = project_root / ".aim" / "targets" / "opencode.toml"
    assert 'name = "name"' in vendored.read_text()
    assert len(updated.history) == 1

    rolled = target_install.rollback(project_root, "a/opencode")
    assert 'name = "name"' not in vendored.read_text()  # back to v1
    assert rolled.current.sha


def test_delete_removes_file_and_entry(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _repo(tmp_path)
    repos.add("a", f"file://{bare}")
    target_install.install(project_root, "a/opencode")

    target_install.delete(project_root, "a/opencode")
    assert not (project_root / ".aim" / "targets" / "opencode.toml").exists()
    assert manifest.load(project_root).targets == []
    assert declarations.load(project_root).targets == []


def test_install_unknown_target_errors(home: Path, project_root: Path) -> None:
    with pytest.raises(target_install.TargetNotIndexedError):
        target_install.install(project_root, "ghost/target")


def test_lock_roundtrips_target(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _repo(tmp_path)
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    target_install.install(project_root, "a/opencode")

    result = asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    assert result.locked_targets == ["a/opencode"]

    m = manifest.load(project_root)
    assert [t.qualified_name for t in m.targets] == ["a/opencode"]
    assert m.targets[0].current.sha
    assert m.targets[0].content_hash

    # Re-locking with no changes is a no-op.
    again = asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    assert again.unchanged is True


def test_sync_reproduces_target_from_lockfile(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _repo(tmp_path)
    repos.add("a", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    target_install.install(project_root, "a/opencode")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))

    vendored = project_root / ".aim" / "targets" / "opencode.toml"
    vendored.unlink()  # simulate a fresh clone that only has the committed lockfile

    result = asyncio.run(sync.run(sync.SyncOptions(project_root=project_root)))
    assert result.synced_targets == ["a/opencode"]
    assert vendored.exists()
    assert 'name = "opencode"' in vendored.read_text()


def test_installed_target_makes_plugin_discoverable(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """The headline value: installing a target makes that client's plugins
    discoverable in the project (via the project-scoped target overlay)."""
    _, target_bare = _repo(tmp_path)
    repos.add("targets", f"file://{target_bare}")
    # An opencode plugin repo. Its kind isn't global, so it registers empty.
    plugin_work = git_fixtures.make_source_repo(
        tmp_path / "psrc", files={"logger/package.json": json.dumps({"name": "logger"})}
    )
    plugin_bare = git_fixtures.make_bare_remote(plugin_work, tmp_path / "pbare.git")
    repos.add("plugins", f"file://{plugin_bare}", allow_empty=True)

    assert plugins.list_plugins(flavor="opencode", project_root=project_root) == []

    target_install.install(project_root, "targets/opencode")

    rows = plugins.list_plugins(flavor="opencode", project_root=project_root)
    assert [r.plugin_name for r in rows] == ["logger"]
