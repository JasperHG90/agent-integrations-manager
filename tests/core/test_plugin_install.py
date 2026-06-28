from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from aim.core import (
    declarations,
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
[manifest]
file = "package.json"
name = "name"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def _install_opencode_kind() -> None:
    """Drop the external opencode kind into the global targets dir (AIM_HOME-isolated)."""
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "opencode.toml").write_text(OPENCODE_KIND_TOML)


# A declarative kind whose vendor_into deliberately escapes the project root.
ESCAPE_KIND_TOML = """
name = "escaper"
[manifest]
file = "package.json"
name = "name"
[register]
vendor_into = "../../escape/{name}"
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


# The source marketplace.json's declared name (`_marketplace_files`).
UPSTREAM = "demo-market"


def _key(repo_alias: str = "a") -> str:
    """The Claude-facing settings.json marketplace key: ``<upstream>-<short id>``.

    Readable (leads with the upstream name) but globally unique (short repo-id
    suffix), so it can't collide with a same-named marketplace in another config
    scope. The vendor dir + lock stay fully id-based (`_mkt()`).
    """
    from aim.core import policy

    return f"{UPSTREAM}-{policy.repo_id_for_url(repos.get(repo_alias).url)[:8]}"


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
    assert mp["name"] == _key()
    assert {p["name"] for p in mp["plugins"]} == {"design-audit"}

    settings = _settings(project_root)
    # Claude-facing key uses the semantic upstream name; the vendor dir path
    # (and the lock, below) stay id-based.
    assert settings["extraKnownMarketplaces"][_key()]["source"]["source"] == "directory"
    assert settings["extraKnownMarketplaces"][_key()]["source"]["path"] == f".claude/plugins/{mkt}"
    assert settings["enabledPlugins"][f"design-audit@{_key()}"] is True

    m = manifest.load(project_root)
    assert [p.qualified_name for p in m.plugins] == ["a/design-audit"]
    assert installed.flavor == "claude"
    assert installed.marketplace_name == mkt  # lock keeps the id-based name
    assert installed.content_hash
    # aim.toml keeps the bare upstream name; the short-id suffix is applied only
    # when composing the Claude-facing key at register time.
    assert declarations.load(project_root).plugins[0].marketplace_name == UPSTREAM


def test_settings_preserves_unmanaged_keys(home: Path, project_root: Path, tmp_path: Path) -> None:
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    settings = _settings(project_root)
    assert "hooks" in settings  # unmanaged key survives
    assert "enabledPlugins" in settings


def _opencode_pkg(name: str = "logger") -> dict[str, str]:
    """An opencode plugin: an npm package directory keyed by its package.json name."""
    return {
        f"{name}/package.json": json.dumps({"name": name}),
        f"{name}/index.ts": "export const plugin = 1\n",
    }


def test_install_opencode_via_external_kind(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=_opencode_pkg())
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_opencode_kind()  # the pluggable kind must be present to discover/install
    repos.add("a", f"file://{bare}")
    installed = plugin_install.install_plugin(project_root, "a/logger")
    vendored = project_root / ".opencode" / "plugins" / "logger"
    assert json.loads((vendored / "package.json").read_text())["name"] == "logger"
    assert (vendored / "index.ts").read_text() == "export const plugin = 1\n"
    assert installed.flavor == "opencode"
    assert installed.marketplace_name is None
    # opencode needs no settings.json registration (the directory drop IS the install).
    assert not (project_root / ".claude" / "settings.json").exists()


def test_install_root_level_manifest(home: Path, project_root: Path, tmp_path: Path) -> None:
    # A repo whose manifest sits at the root (the whole repo is the plugin) must
    # resolve and install — not crash looking for a SKILL.md that isn't there.
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "package.json": json.dumps({"name": "agent-memory"}),
            "index.ts": "export const plugin = 1\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_opencode_kind()
    repos.add("a", f"file://{bare}")
    rows = plugins.list_plugins(flavor="opencode")
    assert [r.plugin_name for r in rows] == ["agent-memory"]
    assert rows[0].source_path == ""  # root-level: the repo itself is the plugin

    plugin_install.install_plugin(project_root, "a/agent-memory")
    vendored = project_root / ".opencode" / "plugins" / "agent-memory"
    assert json.loads((vendored / "package.json").read_text())["name"] == "agent-memory"
    assert (vendored / "index.ts").read_text() == "export const plugin = 1\n"


def test_install_claude_whole_repo_plugin(home: Path, project_root: Path, tmp_path: Path) -> None:
    # The superpowers shape: a root marketplace whose only plugin has source "./",
    # i.e. the whole repo IS the plugin. It must resolve a version (from the nested
    # .claude-plugin/plugin.json, not a non-existent root plugin.json) and vendor the
    # whole repo — discovery alone is not enough; install must work end to end.
    marketplace = {
        "name": "superpowers-dev",
        "plugins": [{"name": "superpowers", "source": "./", "version": "6.0.3"}],
    }
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            ".claude-plugin/plugin.json": json.dumps({"name": "superpowers", "version": "6.0.3"}),
            "skills/tdd/SKILL.md": "# tdd\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("a", f"file://{bare}")

    installed = plugin_install.install_plugin(project_root, "a/superpowers")
    assert installed.flavor == "claude"
    assert installed.source_path == ""
    assert installed.current.tag is not None or installed.current.sha  # version resolved
    assert installed.content_hash

    mkt = _mkt()
    vendored = project_root / ".claude" / "plugins" / mkt / "superpowers"
    assert (vendored / ".claude-plugin" / "plugin.json").exists()
    assert (vendored / "skills" / "tdd" / "SKILL.md").exists()  # whole repo vendored


def test_discover_and_install_via_project_scoped_target(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """A target spec in the PROJECT .aim/targets/ (not the global dir) must let a repo's
    plugins be both discovered AND installed, even though machine-global indexing — which
    only sees built-in + global targets — ignores it."""
    working = git_fixtures.make_source_repo(tmp_path / "src", files=_opencode_pkg())
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
    assert (project_root / ".opencode" / "plugins" / "logger" / "index.ts").read_text() == (
        "export const plugin = 1\n"
    )
    assert installed.flavor == "opencode"


# A manifest-driven kind: gemini-extension.json marks a plugin DIRECTORY; the whole
# directory (context files, MCP config, and all) is vendored.
GEMINI_KIND_TOML = """
name = "gemini"
[manifest]
file = "gemini-extension.json"
name = "name"
[register]
vendor_into = ".gemini/extensions/{name}"
"""


def _install_gemini_kind() -> None:
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "gemini.toml").write_text(GEMINI_KIND_TOML)


def test_install_gemini_extension_vendors_whole_folder(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Manifest-driven: the plugin is named by its gemini-extension.json and the
    # entire extension directory is vendored, so context files and MCP config come too.
    manifest = {
        "name": "weather",
        "version": "1.0.0",
        "contextFileName": "GEMINI.md",
        "mcpServers": {"wx": {"command": "npx", "args": ["-y", "gem-wx"]}},
    }
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "weather/gemini-extension.json": json.dumps(manifest),
            "weather/GEMINI.md": "# Weather context\n",
            "weather/commands/forecast.toml": "prompt = 'forecast'\n",
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_gemini_kind()
    repos.add("a", f"file://{bare}")

    rows = plugins.list_plugins(flavor="gemini")
    assert [r.plugin_name for r in rows] == ["weather"]  # name read from the manifest
    assert rows[0].source_path == "weather"  # the plugin directory, not the marker file

    installed = plugin_install.install_plugin(project_root, "a/weather")
    assert installed.flavor == "gemini"
    ext = project_root / ".gemini" / "extensions" / "weather"
    assert json.loads((ext / "gemini-extension.json").read_text())["name"] == "weather"
    assert (ext / "GEMINI.md").read_text() == "# Weather context\n"  # context file copied
    assert (ext / "commands" / "forecast.toml").exists()  # nested files copied too

    warnings = "\n".join(plugin_install.take_install_warnings())
    assert "npx -y gem-wx" in warnings  # MCP launcher surfaced for review


def test_opencode_unknown_without_external_kind(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=_opencode_pkg())
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
    assert _key() in settings["extraKnownMarketplaces"]
    assert f"design-audit@{_key()}" not in settings["enabledPlugins"]
    assert settings["enabledPlugins"][f"typography@{_key()}"] is True
    assert not (project_root / ".claude" / "plugins" / mkt / "design-audit").exists()

    # Removing the last one drops the marketplace entry entirely.
    plugin_install.delete(project_root, "a/typography")
    settings = _settings(project_root)
    assert _key() not in settings.get("extraKnownMarketplaces", {})
    assert f"typography@{_key()}" not in settings.get("enabledPlugins", {})


def test_delete_warns_about_claude_machine_local_residue(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # aim cleans the committed project surface, but Claude keeps machine-local
    # registry copies it does not GC. delete() must warn the user to purge them.
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    plugin_install.install_plugin(project_root, "a/typography")
    plugin_install.take_install_warnings()  # drain install-time warnings

    # Removing a non-last plugin: warn about the plugin uninstall + its data dir,
    # but NOT the marketplace (still referenced by the survivor).
    plugin_install.delete(project_root, "a/design-audit")
    warns = "\n".join(plugin_install.take_install_warnings())
    assert f"claude plugin uninstall design-audit@{_key()}" in warns
    assert f"rm -rf ~/.claude/plugins/data/design-audit-{_key()}" in warns
    assert "marketplace remove" not in warns  # survivor keeps the marketplace

    # Removing the last plugin: also warn to remove the now-orphaned marketplace.
    plugin_install.delete(project_root, "a/typography")
    warns = "\n".join(plugin_install.take_install_warnings())
    assert f"claude plugin marketplace remove {_key()}" in warns


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
    assert settings["enabledPlugins"][f"design-audit@{_key()}"] is True
    assert _key() in settings["extraKnownMarketplaces"]


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
    assert f"design-audit@{_key()}" not in settings["enabledPlugins"]
    assert settings["enabledPlugins"][f"typography@{_key()}"] is True  # survivor kept
    assert _key() in settings["extraKnownMarketplaces"]  # marketplace refcount survives


def test_install_unknown_plugin_errors(home: Path, project_root: Path) -> None:
    with pytest.raises(plugin_install.plugins.PluginNotIndexedError):
        plugin_install.install_plugin(project_root, "ghost/plugin")


def test_declarative_vendor_into_escape_blocked(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # A declarative kind whose vendor_into escapes the project root is rejected at
    # spec-load time (in addition to the install-time safe_project_path clamp), so
    # it can never be loaded or used to write outside the tree. With no usable kind,
    # the opencode-only repo exposes nothing discoverable.
    working = git_fixtures.make_source_repo(tmp_path / "src", files=_opencode_pkg())
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    _install_escape_kind()
    with pytest.raises(repos.RepoHasNoArtifactsError):
        repos.add("a", f"file://{bare}")
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
    assert settings["enabledPlugins"][f"design-audit@{_key()}"] is True  # present -> registered
    assert f"typography@{_key()}" not in settings["enabledPlugins"]  # missing files -> skipped
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
    `logger` is the same directory carrying a package.json the opencode kind
    discovers by its manifest. The kind must be installed BEFORE `repos.add` so
    discovery indexes both at registration time.
    """
    marketplace = {
        "name": "demo-market",
        "plugins": [{"name": "logger", "source": "./logger", "version": "1.0.0"}],
    }
    files = {
        ".claude-plugin/marketplace.json": json.dumps(marketplace),
        "logger/.claude-plugin/plugin.json": json.dumps({"name": "logger"}),
        "logger/skills/log/SKILL.md": "# log\n",
        "logger/package.json": json.dumps({"name": "logger"}),
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
    # Distinct vendored paths — both vendor the directory, to different destinations.
    assert claude.target_dir != opencode.target_dir
    assert (project_root / claude.target_dir / "skills" / "log" / "SKILL.md").exists()
    assert (
        json.loads(
            (project_root / ".opencode" / "plugins" / "logger" / "package.json").read_text()
        )["name"]
        == "logger"
    )

    m = manifest.load(project_root)
    assert {(p.qualified_name, p.flavor) for p in m.plugins} == {
        ("a/logger", "claude"),
        ("a/logger", "opencode"),
    }

    # A bare lookup spanning two kinds is ambiguous without a flavor.
    with pytest.raises(plugins.PluginAmbiguousFlavorError):
        plugins.index_row("a/logger")


def test_same_upstream_name_distinct_repos_get_distinct_keys(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # Two DIFFERENT repos that both declare a marketplace named "demo-market" must
    # NOT collide on one settings.json key. The short-id suffix makes each key
    # unique (`demo-market-<id_a>` vs `demo-market-<id_b>`), and the bare name is
    # never written, so neither can clash with a same-named marketplace in another
    # config scope either.
    _add_marketplace(tmp_path)  # repo "a": marketplace "demo-market" / design-audit
    other = {
        ".claude-plugin/marketplace.json": json.dumps(
            {
                "name": "demo-market",  # same upstream name, different repo
                "plugins": [{"name": "other-plug", "source": "./other-plug", "version": "1.0.0"}],
            }
        ),
        "other-plug/.claude-plugin/plugin.json": json.dumps({"name": "other-plug"}),
        "other-plug/skills/x/SKILL.md": "# x\n",
    }
    working = git_fixtures.make_source_repo(tmp_path / "srcb", files=other)
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bareb.git")
    repos.add("b", f"file://{bare}")

    plugin_install.install_plugin(project_root, "a/design-audit")
    plugin_install.install_plugin(project_root, "b/other-plug")

    settings = _settings(project_root)
    key_a, key_b = _key("a"), _key("b")
    assert key_a != key_b
    assert UPSTREAM not in settings["extraKnownMarketplaces"]  # bare name never written
    assert settings["enabledPlugins"][f"design-audit@{key_a}"] is True
    assert settings["enabledPlugins"][f"other-plug@{key_b}"] is True
    # Distinct keys point at distinct (id-based) vendor dirs.
    assert (
        settings["extraKnownMarketplaces"][key_a]["source"]["path"]
        == f".claude/plugins/{_mkt('a')}"
    )
    assert (
        settings["extraKnownMarketplaces"][key_b]["source"]["path"]
        == f".claude/plugins/{_mkt('b')}"
    )


def test_upgrade_replaces_id_key_with_semantic(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    # A pre-semantic install left an id-form key in settings.json; a later register
    # must self-heal to a single semantic entry (no duplicate registration).
    _add_marketplace(tmp_path)
    plugin_install.install_plugin(project_root, "a/design-audit")
    mkt = _mkt()
    settings_path = project_root / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    data["extraKnownMarketplaces"][mkt] = {
        "source": {"source": "directory", "path": f".claude/plugins/{mkt}"}
    }
    data["enabledPlugins"][f"design-audit@{mkt}"] = True
    settings_path.write_text(json.dumps(data))

    plugin_install.reconcile_registration(project_root, manifest.load(project_root))

    settings = _settings(project_root)
    assert mkt not in settings["extraKnownMarketplaces"]  # stale id-form retired
    assert f"design-audit@{mkt}" not in settings["enabledPlugins"]
    assert _key() in settings["extraKnownMarketplaces"]
    assert settings["enabledPlugins"][f"design-audit@{_key()}"] is True
