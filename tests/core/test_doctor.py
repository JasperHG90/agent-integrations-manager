from __future__ import annotations

import asyncio
from pathlib import Path

from aim.core import doctor, install, repos, roots
from aim.core import init as init_mod
from aim.core import sync as sync_mod
from aim.core.lock import LockOptions
from aim.core.lock import run as lock_run
from tests.fixtures import git_fixtures


def _run_lock_and_sync(project_root: Path) -> None:
    asyncio.run(lock_run(LockOptions(project_root=project_root)))
    asyncio.run(sync_mod.run(sync_mod.SyncOptions(project_root=project_root)))


def _bare_with_skill(tmp_path: Path) -> Path:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    return git_fixtures.make_bare_remote(working, tmp_path / "bare.git")


def test_doctor_clean_project_no_findings(home: Path, project_root: Path, tmp_path: Path) -> None:
    bare = _bare_with_skill(tmp_path)
    repos.add("anth", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")

    report = doctor.audit(project_roots=[project_root])
    assert report.projects_audited == 1
    errors = report.by_severity("error")
    warnings = report.by_severity("warning")
    assert errors == []
    # Newly-fetched repo, no edits — no warnings from project either.
    assert all("anth/foo" not in f.message for f in warnings)


def test_doctor_detects_skill_drift(home: Path, project_root: Path, tmp_path: Path) -> None:
    bare = _bare_with_skill(tmp_path)
    repos.add("anth", f"file://{bare}")
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    install.install(project_root, "anth/foo")

    (project_root / ".claude" / "skills" / "foo" / "SKILL.md").write_text("hand-edit\n")
    report = doctor.audit(project_roots=[project_root])
    msgs = [f.message for f in report.by_severity("warning")]
    assert any("edited since install" in m for m in msgs)


def test_doctor_detects_region_drift(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    _run_lock_and_sync(project_root)
    # Edit inside the header region marker.
    agents = project_root / "AGENTS.md"
    text = agents.read_text()
    edited = text.replace("Agent instructions", "edited inside marker")
    agents.write_text(edited)
    report = doctor.audit(project_roots=[project_root])
    assert any("region 'header' edited" in f.message for f in report.by_severity("warning"))


def test_doctor_detects_missing_target_dir(home: Path, project_root: Path, tmp_path: Path) -> None:
    bare = _bare_with_skill(tmp_path)
    repos.add("anth", f"file://{bare}")
    install.install(project_root, "anth/foo")
    import shutil

    shutil.rmtree(project_root / ".claude" / "skills" / "foo")
    report = doctor.audit(project_roots=[project_root])
    assert any(
        "target" in f.message and "missing" in f.message for f in report.by_severity("error")
    )
    assert not report.ok


def test_doctor_uses_configured_roots_when_none_passed(home: Path, project_root: Path) -> None:
    init_mod.run(init_mod.InitOptions(project_root=project_root))
    roots.add_root(project_root)
    report = doctor.audit()
    assert report.projects_audited == 1


def test_doctor_warns_on_partial_snapshot(home: Path, project_root: Path, tmp_path: Path) -> None:
    from aim.core import paths

    bare = _bare_with_skill(tmp_path)
    repos.add("anth", f"file://{bare}")
    installed = install.install(project_root, "anth/foo")
    snap = paths.snapshots_cache_dir() / "anth" / installed.current.sha / "foo"
    (snap / ".aim.complete").unlink()

    report = doctor.audit()
    assert any("missing .aim.complete" in f.message for f in report.by_severity("warning"))


def test_roots_round_trip(home: Path, tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    roots.add_root(a)
    roots.add_root(b)
    listed = [str(r) for r in roots.list_roots()]
    assert str(a.resolve()) in listed
    assert str(b.resolve()) in listed
    roots.add_root(a)  # idempotent
    assert len(roots.list_roots()) == 2
    assert roots.remove_root(a)
    assert str(a.resolve()) not in [str(r) for r in roots.list_roots()]
