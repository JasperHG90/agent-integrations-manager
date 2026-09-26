from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import content_guard, install, manifest, paths, repos
from tests.fixtures import git_fixtures


def _build_repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    return working, bare


def test_install_first_writes_files_and_manifest(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _build_repo(
        tmp_path,
        {
            "skills/foo/SKILL.md": "# foo\n\nDescribed.\n",
            "skills/foo/extra.md": "auxiliary\n",
        },
    )
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    target = project_root / ".claude" / "skills" / "foo"
    assert (target / "SKILL.md").read_text().startswith("# foo")
    assert (target / "extra.md").read_text() == "auxiliary\n"
    m = manifest.load(project_root)
    assert len(m.skills) == 1
    assert m.skills[0].qualified_name == "a/foo"
    assert m.skills[0].source_path == "skills/foo"
    assert m.skills[0].current.sha
    assert m.skills[0].history == []


def test_install_writes_snapshot(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/foo")
    snap = paths.snapshots_cache_dir() / "a" / installed.current.sha / "foo"
    assert (snap / "SKILL.md").exists()


def test_install_unknown_skill_errors(home: Path, project_root: Path) -> None:
    with pytest.raises(install.SkillNotIndexedError):
        install.install(project_root, "ghost/skill")


def test_install_with_tag_records_tag(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    git_fixtures.add_tag(working, "v1.0.0")
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/foo")
    assert installed.current.tag == "v1.0.0"
    assert installed.current.identifier().startswith("v1.0.0+")


def test_update_when_upstream_unchanged_is_noop(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    initial_sha = manifest.load(project_root).skills[0].current.sha

    install.update(project_root, "a/foo")
    m = manifest.load(project_root)
    assert m.skills[0].current.sha == initial_sha
    assert m.skills[0].history == []


def _add_binary_file_commit(working: Path, rel_path: str, content: bytes, message: str) -> None:
    path = working / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    import subprocess

    subprocess.run(["git", "add", "."], cwd=working, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=working, check=True, capture_output=True
    )


def test_lock_handles_binary_skill_files(home: Path, project_root: Path, tmp_path: Path) -> None:
    """Skills may contain binary assets; hashing them must not assume UTF-8."""
    import asyncio

    from aim.core.lock import LockOptions
    from aim.core.lock import run as lock_run

    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n"},
    )
    # Add a binary asset containing a Windows-1252 smart quote byte (0x93).
    _add_binary_file_commit(
        working,
        "skills/foo/asset.bin",
        b"\x00\x01\x02\x93\xff",
        "add binary asset",
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    # Lock re-hashes skill contents and must tolerate the binary file.
    asyncio.run(lock_run(LockOptions(project_root=project_root)))
    m = manifest.load(project_root)
    assert m.skills[0].qualified_name == "a/foo"
    assert m.skills[0].content_hash


def test_update_after_upstream_change_pushes_history(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    v1_sha = manifest.load(project_root).skills[0].current.sha

    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")

    install.update(project_root, "a/foo")
    m = manifest.load(project_root)
    assert m.skills[0].current.sha != v1_sha
    assert m.skills[0].history[0].sha == v1_sha
    target = project_root / ".claude" / "skills" / "foo"
    assert (target / "SKILL.md").read_text() == "# v2\n"


def test_update_refuses_when_source_path_moved(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")

    # Move the skill: delete old, add new at different path. Both still named "foo".
    (working / "skills" / "foo" / "SKILL.md").unlink()
    (working / "skills" / "foo").rmdir()
    git_fixtures.add_commit(
        working,
        {".claude/skills/foo/SKILL.md": "# moved\n"},
        "move foo",
    )
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")

    with pytest.raises(install.SkillSourcePathChangedError):
        install.update(project_root, "a/foo")


def test_delete_removes_target_and_manifest_entry(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    target = project_root / ".claude" / "skills" / "foo"
    assert target.exists()

    install.delete(project_root, "a/foo")
    assert not target.exists()
    m = manifest.load(project_root)
    assert m.skills == []


def test_uninstall_removes_target_and_manifest_entry(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    target = project_root / ".claude" / "skills" / "foo"
    assert target.exists()

    install.delete(project_root, "a/foo")
    assert not target.exists()
    m = manifest.load(project_root)
    assert m.skills == []


def test_delete_unknown_errors(home: Path, project_root: Path) -> None:
    with pytest.raises(install.SkillNotInstalledError):
        install.delete(project_root, "ghost/skill")


def test_rollback_restores_previous_version(home: Path, project_root: Path, tmp_path: Path) -> None:
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    v1_sha = manifest.load(project_root).skills[0].current.sha

    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")
    install.update(project_root, "a/foo")

    rolled = install.rollback(project_root, "a/foo")
    assert rolled.current.sha == v1_sha
    target = project_root / ".claude" / "skills" / "foo"
    assert (target / "SKILL.md").read_text() == "# v1\n"


def test_rollback_without_history_errors(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    with pytest.raises(install.NoHistoryToRollbackError):
        install.rollback(project_root, "a/foo")


def test_rollback_works_from_local_snapshot_even_if_upstream_lost(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """The whole point of the local snapshot: rollback should not require git."""
    working, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# v1\n"})
    repos.add("a", f"file://{bare}")
    install.install(project_root, "a/foo")
    v1_sha = manifest.load(project_root).skills[0].current.sha

    git_fixtures.add_commit(working, {"skills/foo/SKILL.md": "# v2\n"}, "v2")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")
    install.update(project_root, "a/foo")

    # Wipe the cached clone and the upstream bare repo — only the snapshot remains.
    import shutil

    shutil.rmtree(repos.clone_dir("a"))
    shutil.rmtree(bare)

    rolled = install.rollback(project_root, "a/foo")
    assert rolled.current.sha == v1_sha
    target = project_root / ".claude" / "skills" / "foo"
    assert (target / "SKILL.md").read_text() == "# v1\n"


def test_install_plugin_style_skill(home: Path, project_root: Path, tmp_path: Path) -> None:
    """Skills nested under plugins/<cat>/skills/<name> install correctly."""
    _, bare = _build_repo(
        tmp_path,
        {
            "plugins/business-analytics/skills/data-storytelling/SKILL.md": (
                "# Data Storytelling\n\nTell stories.\n"
            ),
            "plugins/business-analytics/skills/data-storytelling/helper.md": "helper\n",
        },
    )
    repos.add("wshobson", f"file://{bare}")
    install.install(project_root, "wshobson/data-storytelling")

    target = project_root / ".claude" / "skills" / "data-storytelling"
    assert (target / "SKILL.md").read_text().startswith("# Data Storytelling")
    assert (target / "helper.md").read_text() == "helper\n"
    m = manifest.load(project_root)
    assert m.skills[0].source_path == "plugins/business-analytics/skills/data-storytelling"


def test_install_override_risk_allows_hidden_unicode(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _, bare = _build_repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n\nhidden\u200b\n"})
    repos.add("a", f"file://{bare}")
    installed = install.install(project_root, "a/foo", override_risk=True)
    assert installed.risk_acknowledged
    skill_md = project_root / ".claude" / "skills" / "foo" / "SKILL.md"
    assert skill_md.read_text() == "# foo\n\nhidden\u200b\n"


def test_install_rejects_hidden_unicode(home: Path, project_root: Path, tmp_path: Path) -> None:
    _, bare = _build_repo(
        tmp_path,
        {"skills/foo/SKILL.md": "# foo\n\nhidden\u200b\n"},
    )
    repos.add("a", f"file://{bare}")
    with pytest.raises(content_guard.HiddenUnicodeError):
        install.install(project_root, "a/foo")
    assert not (project_root / ".claude" / "skills" / "foo").exists()


def test_install_plugin_owned_skill_emits_notice(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """Installing a plugin-bundled skill standalone works but notes the plugin."""
    import json

    marketplace = {"name": "demo", "plugins": [{"name": "bundler", "source": "./bundler"}]}
    _, bare = _build_repo(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "bundler/.claude-plugin/plugin.json": json.dumps({"name": "bundler"}),
            "bundler/skills/inner/SKILL.md": "# inner\n",
        },
    )
    repos.add("a", f"file://{bare}")
    install.take_install_warnings()  # drain any leftovers
    install.install(project_root, "a/inner")
    warnings = install.take_install_warnings()
    assert any("bundled with plugin a/bundler" in w for w in warnings)
    assert (project_root / ".claude" / "skills" / "inner" / "SKILL.md").exists()


def test_skill_version_sha_must_be_hex() -> None:
    """A lockfile-supplied sha is untrusted input that reaches git argv and
    cache paths — the model boundary rejects anything but a git object id."""
    from datetime import UTC, datetime

    from aim.core.models import SkillVersion

    good = SkillVersion(sha="a" * 40, installed_at=datetime.now(UTC))
    assert good.sha == "a" * 40
    for bad in ("--output=/tmp/x", "../../../etc", "HEAD", "main", "ABC1234", "", "a" * 65):
        with pytest.raises(ValueError, match="not a git object id"):
            SkillVersion(sha=bad, installed_at=datetime.now(UTC))


def test_gather_skill_text_reports_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content past the risk-scan cap must be reported, not silently dropped —
    filler files sort first, so a payload could otherwise ship unscanned."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "aaa-filler.md").write_text("x" * 100)
    (snap / "zzz-payload.md").write_text("EVILMARKER")
    monkeypatch.setattr(install, "_RISK_TEXT_CAP", 50)
    text, truncated = install._gather_skill_text(snap)
    assert truncated is True
    assert "EVILMARKER" not in text  # the classifier never saw it — hence the flag

    monkeypatch.setattr(install, "_RISK_TEXT_CAP", 1024)
    text, truncated = install._gather_skill_text(snap)
    assert truncated is False
    assert "EVILMARKER" in text


def test_gate_oversized_fails_closed_in_block_mode(home: Path) -> None:
    from aim.core import policy as policy_mod
    from aim.core import risk

    pol = policy_mod.Policy(name="p", risk=policy_mod.RiskSettings(classifier=True, mode="block"))
    with pytest.raises(risk.RiskBlockedError, match="exceeds the risk-scan cap"):
        risk.gate_oversized(source="a/big", pol=pol, kind="skill")
    # Advisory mode warns instead of raising.
    pol_warn = policy_mod.Policy(
        name="p", risk=policy_mod.RiskSettings(classifier=True, mode="warn")
    )
    risk.take_risk_warnings()
    risk.gate_oversized(source="a/big", pol=pol_warn, kind="skill")
    assert any("risk-scan cap" in w for w in risk.take_risk_warnings())


def test_snapshot_dir_rejects_traversal_components(home: Path) -> None:
    """Defense in depth: `_ensure_snapshot` rmtree's this path, so components
    must be single safe segments even if upstream validation regresses."""
    for bad in ("../escape", "a/b", "..", "", "x\\y"):
        with pytest.raises(ValueError, match="unsafe snapshot path component"):
            install._snapshot_dir("repo", bad, "skill")
        with pytest.raises(ValueError, match="unsafe snapshot path component"):
            install._snapshot_dir(bad, "d" * 40, "skill")
    snap = install._snapshot_dir("repo", "d" * 40, "skill")
    assert snap == paths.snapshots_cache_dir() / "repo" / ("d" * 40) / "skill"
