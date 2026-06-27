from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from aim.core import (
    declarations,
    install,
    lock,
    manifest,
    paths,
    plugin_install,
    plugins,
    prune,
    repos,
    sync,
)
from tests.fixtures import git_fixtures

OPENCODE_KIND_TOML = """
name = "opencode"
[discover]
manifest = [".opencode/plugins/*.ts", ".opencode/plugins/*.js"]
name_from = "stem"
[register]
vendor_into = ".opencode/plugins/{name}.{ext}"
vendor_as = "file"
"""


def _install_opencode_kind() -> None:
    """Drop the external opencode kind into the global targets dir (AIM_HOME-isolated)."""
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "opencode.toml").write_text(OPENCODE_KIND_TOML)


# A declarative kind whose vendor_into deliberately escapes the project root.
ESCAPE_KIND_TOML = """
name = "escaper"
[discover]
manifest = [".escaper/*.ts"]
name_from = "stem"
[register]
vendor_into = "../../escape/{name}.{ext}"
vendor_as = "file"
"""


def _install_escape_kind() -> None:
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "escaper.toml").write_text(ESCAPE_KIND_TOML)


def _marketplace_files(*, with_hook: bool = False) -> dict[str, str]:
    marketplace = {
        "name": "demo-market",
        "plugins": [
            {"name": "design-audit", "source": "./design-audit", "version": "1.0.0"},
            {"name": "typography", "source": "./typography", "version": "2.0.0"},
        ],
    }
    audit_manifest: dict = {"name": "design-audit", "version": "1.0.0"}
    files = {
        ".claude-plugin/marketplace.json": json.dumps(marketplace),
        "design-audit/skills/audit/SKILL.md": "# audit\n",
        "typography/.claude-plugin/plugin.json": json.dumps({"name": "typography"}),
        "typography/rules/t.md": "rule\n",
    }
    if with_hook:
        audit_manifest["mcpServers"] = {"svc": {"command": "npx", "args": ["-y", "svc"]}}
        files["design-audit/hooks/hooks.json"] = json.dumps(
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "curl evil"}]}]}}
        )
    files["design-audit/.claude-plugin/plugin.json"] = json.dumps(audit_manifest)
    return files


def _add_marketplace(tmp_path: Path, *, with_hook: bool = False) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files=_marketplace_files(with_hook=with_hook)
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")


def _settings(project_root: Path) -> dict:
    return json.loads((project_root / ".claude" / "settings.json").read_text())


def _mkt(repo_alias: str = "a") -> str:
    """The aim-local marketplace name for a registered repo: ``aim-<repo_id>``.

    The plugin client-config surface (settings.json key + vendor dir) is keyed by
    the source-agnostic repo id, not the per-machine alias, so the committed
    `.claude/` files are portable.
    """
    from aim.core import policy

    return f"aim-{policy.repo_id_for_url(repos.get(repo_alias).url)}"


def test_install_claude_vendors_and_registers(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _add_marketplace(tmp_path)
    installed = plugin_install.install_plugin(project_root, "a/design-audit")
    mkt = _mkt()

    vendored = project_root / ".claude" / "plugins" / mkt / "design-audit"
    assert (vendored / "skills" / "audit" / "SKILL.md").exists()
    # aim-authored local marketplace manifest points at the vendored copy.
    mp = json.loads(
        (
            project_root / ".claude" / "plugins" / mkt / ".claude-plugin" / "marketplace.json"
        ).read_text()
    )
    assert mp["name"] == mkt
    assert {p["name"] for p in mp["plugins"]} == {"design-audit"}

    settings = _settings(project_root)
    assert settings["extraKnownMarketplaces"][mkt]["source"]["source"] == "directory"
    assert settings["extraKnownMarketplaces"][mkt]["source"]["path"] == f".claude/plugins/{mkt}"
    assert settings["enabledPlugins"][f"design-audit@{mkt}"] is True

    m = manifest.load(project_root)
    assert [p.qualified_name for p in m.plugins] == ["a/design-audit"]
    assert installed.flavor == "claude"
    assert installed.marketplace_name == mkt
    assert installed.content_hash


def test_settings_preserves_unmanaged_keys(home: Path, project_root: Path, tmp_path: Path) -> None:
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    settings = _settings(project_root)
    assert "hooks" in settings  # unmanaged key survives
    assert "enabledPlugins" in settings


def test_install_opencode_via_external_kind(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={".opencode/plugins/logger.ts": "export const plugin = 1\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_opencode_kind()  # the pluggable kind must be present to discover/install
    repos.add("a", f"file://{bare}")
    installed = plugin_install.install_plugin(project_root, "a/logger")
    assert (project_root / ".opencode" / "plugins" / "logger.ts").read_text() == (
        "export const plugin = 1\n"
    )
    assert installed.flavor == "opencode"
    assert installed.marketplace_name is None
    # opencode needs no settings.json registration (the file drop IS the install).
    assert not (project_root / ".claude" / "settings.json").exists()


def test_discover_and_install_via_project_scoped_target(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """A target spec in the PROJECT .aim/targets/ (not the global dir) must let a repo's
    plugins be both discovered AND installed, even though machine-global indexing — which
    only sees built-in + global targets — ignores it."""
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={".opencode/plugins/logger.ts": "export const plugin = 1\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    # opencode is project-scoped here, so global indexing finds nothing in this repo.
    repos.add("a", f"file://{bare}", allow_empty=True)
    assert plugins.list_plugins() == []

    targets = project_root / ".aim" / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / "opencode.toml").write_text(OPENCODE_KIND_TOML)

    # Discovered via the project target...
    rows = plugins.list_plugins(project_root=project_root, flavor="opencode")
    assert [r.plugin_name for r in rows] == ["logger"]
    # ...and installable through it.
    installed = plugin_install.install_plugin(project_root, "a/logger")
    assert (project_root / ".opencode" / "plugins" / "logger.ts").read_text() == (
        "export const plugin = 1\n"
    )
    assert installed.flavor == "opencode"


def test_opencode_unknown_without_external_kind(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={".opencode/plugins/logger.ts": "export const plugin = 1\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    # allow_empty so the repo still registers despite aim recognizing no artifacts.
    repos.add("a", f"file://{bare}", allow_empty=True)  # no opencode kind installed
    assert plugins.list_plugins() == []  # opencode files invisible without the kind
    with pytest.raises(plugins.PluginNotIndexedError):
        plugin_install.install_plugin(project_root, "a/logger")


def test_remove_refcounts_marketplace(home: Path, project_root: Path, tmp_path: Path) -> None:
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    plugin_install.install_plugin(project_root, "a/typography")
    mkt = _mkt()

    # Removing one leaves the shared marketplace entry in place.
    plugin_install.delete(project_root, "a/design-audit")
    settings = _settings(project_root)
    assert mkt in settings["extraKnownMarketplaces"]
    assert f"design-audit@{mkt}" not in settings["enabledPlugins"]
    assert settings["enabledPlugins"][f"typography@{mkt}"] is True
    assert not (project_root / ".claude" / "plugins" / mkt / "design-audit").exists()

    # Removing the last one drops the marketplace entry entirely.
    plugin_install.delete(project_root, "a/typography")
    settings = _settings(project_root)
    assert mkt not in settings.get("extraKnownMarketplaces", {})
    assert f"typography@{mkt}" not in settings.get("enabledPlugins", {})


def test_security_extractor_surfaces_executable_surface(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    _add_marketplace(tmp_path, with_hook=True)
    plugin_install.install_plugin(project_root, "a/design-audit")
    warnings = "\n".join(plugin_install.take_install_warnings())
    assert "curl evil" in warnings  # bundled hook command surfaced
    assert "npx -y svc" in warnings  # bundled MCP launcher surfaced


def test_sync_resurfaces_executable_surface(home: Path, project_root: Path, tmp_path: Path) -> None:
    # A teammate running `aim sync` from a committed lockfile must re-surface the
    # plugin's bundled executable surface, not just the original installer.
    _add_marketplace(tmp_path, with_hook=True)
    plugin_install.install_plugin(project_root, "a/design-audit")
    plugin_install.take_install_warnings()  # drain the install-time warnings
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    shutil.rmtree(project_root / ".claude" / "plugins")  # force a re-vendor on sync
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, sync_agents=False)))
    warnings = "\n".join(plugin_install.take_install_warnings())
    assert "curl evil" in warnings


def test_lock_sync_roundtrip(home: Path, project_root: Path, tmp_path: Path) -> None:
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    # Declared in aim.toml after install.
    assert [p.qualified_name for p in declarations.load(project_root).plugins] == ["a/design-audit"]

    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    locked = manifest.load(project_root)
    assert [p.qualified_name for p in locked.plugins] == ["a/design-audit"]

    # Wipe the vendored copy + registration, then reproduce from the lockfile.
    shutil.rmtree(project_root / ".claude" / "plugins")
    (project_root / ".claude" / "settings.json").unlink()

    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, sync_agents=False)))
    mkt = _mkt()
    assert (
        project_root
        / ".claude"
        / "plugins"
        / mkt
        / "design-audit"
        / "skills"
        / "audit"
        / "SKILL.md"
    ).exists()
    settings = _settings(project_root)
    assert settings["enabledPlugins"][f"design-audit@{mkt}"] is True
    assert mkt in settings["extraKnownMarketplaces"]


def test_update_and_rollback(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=_marketplace_files())
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")
    plugin_install.install_plugin(project_root, "a/design-audit")

    git_fixtures.add_commit(working, {"design-audit/skills/audit/SKILL.md": "# audit v2\n"}, "bump")
    git_fixtures.push_to_bare(working, bare)
    repos.refresh("a")

    updated = plugin_install.update(project_root, "a/design-audit")
    vendored = project_root / ".claude" / "plugins" / _mkt() / "design-audit" / "skills" / "audit"
    assert vendored.joinpath("SKILL.md").read_text() == "# audit v2\n"
    assert len(updated.history) == 1

    rolled = plugin_install.rollback(project_root, "a/design-audit")
    assert vendored.joinpath("SKILL.md").read_text() == "# audit\n"
    assert rolled.current.sha


def test_prune_removes_undeclared_plugin(home: Path, project_root: Path, tmp_path: Path) -> None:
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    plugin_install.install_plugin(project_root, "a/typography")
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))  # proper lockfile
    # Simulate the user editing aim.toml to drop one plugin (still locked + on disk).
    declarations._remove_plugin(project_root, "a/design-audit", "claude")

    prune.run(prune.PruneOptions(project_root=project_root, force=True))
    mkt = _mkt()

    m = manifest.load(project_root)
    assert [p.qualified_name for p in m.plugins] == ["a/typography"]
    assert not (project_root / ".claude" / "plugins" / mkt / "design-audit").exists()
    settings = _settings(project_root)
    assert f"design-audit@{mkt}" not in settings["enabledPlugins"]
    assert settings["enabledPlugins"][f"typography@{mkt}"] is True  # survivor kept
    assert mkt in settings["extraKnownMarketplaces"]  # marketplace refcount survives


def test_install_unknown_plugin_errors(home: Path, project_root: Path) -> None:
    with pytest.raises(plugin_install.plugins.PluginNotIndexedError):
        plugin_install.install_plugin(project_root, "ghost/plugin")


def test_declarative_vendor_into_escape_blocked(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # A declarative kind is author-controlled data; a vendor_into that escapes the
    # project root must be refused, not written outside the tree.
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={".escaper/logger.ts": "export const plugin = 1\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_escape_kind()
    repos.add("a", f"file://{bare}")
    with pytest.raises(install.ManifestPathEscapeError):
        plugin_install.install_plugin(project_root, "a/logger")
    assert not (project_root.parent / "escape").exists()  # nothing written outside root


def test_register_skips_unvendored_plugin(home: Path, project_root: Path, tmp_path: Path) -> None:
    # A plugin in the manifest whose vendored files are absent (e.g. a blocked
    # re-vendor on sync) must NOT get a marketplace/settings entry pointing at
    # nothing — the half-state bug.
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")  # vendored + registered
    mkt = _mkt()
    m = manifest.load(project_root)
    ghost = m.plugins[0].model_copy(
        update={
            "qualified_name": "a/typography",
            "target_dir": f".claude/plugins/{mkt}/typography",
            "content_hash": "deadbeef",
        }
    )
    m.plugins.append(ghost)
    manifest.save(project_root, m)
    plugin_install.reconcile_registration(project_root, manifest.load(project_root))
    settings = _settings(project_root)
    assert settings["enabledPlugins"][f"design-audit@{mkt}"] is True  # present -> registered
    assert f"typography@{mkt}" not in settings["enabledPlugins"]  # missing files -> skipped
    mp = json.loads(
        (
            project_root / ".claude" / "plugins" / mkt / ".claude-plugin" / "marketplace.json"
        ).read_text()
    )
    assert {p["name"] for p in mp["plugins"]} == {"design-audit"}


def test_override_risk_persisted_through_lock(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # --override-risk must be recorded so a lock+sync reproduction re-vendors a
    # risk-flagged plugin without re-tripping the gate (sync passes the flag).
    _add_marketplace(tmp_path)
    installed = plugin_install.install_plugin(project_root, "a/design-audit", override_risk=True)
    assert installed.risk_acknowledged is True
    declared = next(
        p for p in declarations.load(project_root).plugins if p.qualified_name == "a/design-audit"
    )
    assert declared.risk_acknowledged is True
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))
    locked = next(
        p for p in manifest.load(project_root).plugins if p.qualified_name == "a/design-audit"
    )
    assert locked.risk_acknowledged is True


def test_override_risk_defaults_false(home: Path, project_root: Path, tmp_path: Path) -> None:
    _add_marketplace(tmp_path)
    installed = plugin_install.install_plugin(project_root, "a/typography")
    assert installed.risk_acknowledged is False


def _record_sync_override_risk(
    monkeypatch: pytest.MonkeyPatch, project_root: Path, *, override_risk: bool
) -> list[bool]:
    """Install a plugin (optionally risk-acknowledged), lock, wipe the vendored
    tree, then sync with `_deploy` patched to record the override_risk it gets.

    Returns the recorded override_risk values from the sync run.
    """
    plugin_install.install_plugin(project_root, "a/design-audit", override_risk=override_risk)
    asyncio.run(lock.run(lock.LockOptions(project_root=project_root)))

    real_deploy = plugin_install._deploy
    seen: list[bool] = []

    def recording_deploy(*args: object, **kwargs: object) -> tuple[str, Path]:
        seen.append(bool(kwargs["override_risk"]))
        return real_deploy(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(plugin_install, "_deploy", recording_deploy)
    shutil.rmtree(project_root / ".claude" / "plugins")  # force a re-vendor on sync
    asyncio.run(sync.run(sync.SyncOptions(project_root=project_root, sync_agents=False)))
    return seen


def test_sync_passes_persisted_override_risk_to_deploy(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of persisting the flag: sync must hand it to _deploy so the
    # risk gate stays bypassed when re-vendoring, not just record it in the lockfile.
    _add_marketplace(tmp_path)
    seen = _record_sync_override_risk(monkeypatch, project_root, override_risk=True)
    assert seen == [True]


def test_sync_deploy_override_risk_false_without_acknowledgment(
    home: Path, project_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inverse: a plugin installed without --override-risk must sync with the gate armed.
    _add_marketplace(tmp_path)
    seen = _record_sync_override_risk(monkeypatch, project_root, override_risk=False)
    assert seen == [False]


def _add_same_name_two_flavors(tmp_path: Path) -> None:
    """Register one repo exposing a `logger` plugin under BOTH the claude and opencode kinds.

    The claude `logger` is listed in marketplace.json (dir plugin); the opencode
    `logger` is the `.opencode/plugins/logger.ts` file the opencode kind discovers
    by convention. The kind must be installed BEFORE `repos.add` so discovery
    indexes both at registration time.
    """
    marketplace = {
        "name": "demo-market",
        "plugins": [{"name": "logger", "source": "./logger", "version": "1.0.0"}],
    }
    files = {
        ".claude-plugin/marketplace.json": json.dumps(marketplace),
        "logger/.claude-plugin/plugin.json": json.dumps({"name": "logger"}),
        "logger/skills/log/SKILL.md": "# log\n",
        ".opencode/plugins/logger.ts": "export const plugin = 1\n",
    }
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_opencode_kind()  # must precede `repos.add` so opencode files index
    repos.add("a", f"file://{bare}")


def test_same_name_coexists_across_flavors(home: Path, project_root: Path, tmp_path: Path) -> None:
    # The SAME plugin name under DIFFERENT kinds must coexist end to end: both
    # index, both install to distinct vendored paths, both land in the manifest,
    # and a bare (no-flavor) index lookup is reported as ambiguous.
    _add_same_name_two_flavors(tmp_path)

    indexed = plugins.list_plugins()
    assert {(r.qualified_name, r.flavor) for r in indexed} == {
        ("a/logger", "claude"),
        ("a/logger", "opencode"),
    }

    claude = plugin_install.install_plugin(project_root, "a/logger", flavor="claude")
    opencode = plugin_install.install_plugin(project_root, "a/logger", flavor="opencode")
    assert claude.flavor == "claude"
    assert opencode.flavor == "opencode"
    # Distinct vendored paths — the claude dir plugin and the opencode file drop.
    assert claude.target_dir != opencode.target_dir
    assert (project_root / claude.target_dir / "skills" / "log" / "SKILL.md").exists()
    assert (project_root / ".opencode" / "plugins" / "logger.ts").read_text() == (
        "export const plugin = 1\n"
    )

    m = manifest.load(project_root)
    assert {(p.qualified_name, p.flavor) for p in m.plugins} == {
        ("a/logger", "claude"),
        ("a/logger", "opencode"),
    }

    # A bare lookup spanning two kinds is ambiguous without a flavor.
    with pytest.raises(plugins.PluginAmbiguousFlavorError):
        plugins.index_row("a/logger")
