"""Plugin install / update / delete / rollback — the kind-agnostic orchestrator.

A plugin is vendored (copied) into the project at a locked SHA, exactly like a
skill. aim core owns the guarantees here — ref→SHA, snapshot, security gate,
vendoring through ``safe_project_path``, content-hash, lockfile — and delegates
the client-specific parts to the plugin **kind** (`plugin_kinds`):

- ``kind.source_unit`` (``"dir"`` | ``"file"``) — what bytes are the plugin.
- ``kind.vendor_target(...)`` — where the bytes are vendored.
- ``kind.register(...)`` / ``kind.unregister(...)`` — client config (e.g.
  ``.claude/settings.json``), reconciled from the installed set.

Built-in kinds (claude) ship with aim; external declarative kinds (opencode) are
TOML specs in a kinds dir. The orchestrator never branches on a specific kind.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from aim.core import (
    content_guard,
    declarations,
    git,
    hashing,
    install,
    manifest,
    paths,
    plugin_kinds,
    plugins,
    policy,
    repos,
    risk,
)
from aim.core.models import InstalledPlugin, Manifest, SkillVersion


class PluginNotInstalledError(KeyError):
    """No entry for this plugin in the project manifest."""

    def __str__(self) -> str:
        name = self.args[0] if self.args else "plugin"
        return f"{name} is not installed in this project"


class PluginFlavorUnsupportedError(RuntimeError):
    """No kind is loaded for this plugin's flavor (e.g. an external kind is missing)."""


class PluginSourcePathChangedError(RuntimeError):
    """On `update`, the plugin's source_path inside its repo moved upstream."""


class PluginPinError(RuntimeError):
    """A user-supplied --pin value is not a resolvable git ref/sha in the repo."""


_install_warnings: list[str] = []


def take_install_warnings() -> list[str]:
    """Drain the install-warning buffer (executable-surface notices). CLI/TUI surface these."""
    out = list(_install_warnings)
    _install_warnings.clear()
    return out


def _kind_for(name: str, project_root: Path) -> plugin_kinds.PluginKind:
    """Return the loaded kind for ``name``, or raise if none is installed."""
    kind = plugin_kinds.get_kind(name, project_root)
    if kind is None:
        raise PluginFlavorUnsupportedError(
            f"no plugin kind {name!r} is loaded; install its kind spec (.aim/kinds or the "
            "global kinds dir) or upgrade aim"
        )
    return kind


def _load_manifest(project_root: Path) -> Manifest:
    """Load the project manifest, creating an empty one if absent."""
    return manifest.load_or_create(project_root)


def _find_installed(
    m: Manifest, qualified_name: str, flavor: str | None = None
) -> InstalledPlugin | None:
    """Return the manifest's installed entry for a plugin, or None if absent.

    A name can be installed under more than one flavor; pass ``flavor`` to
    disambiguate. Raises PluginAmbiguousFlavorError if several entries share the
    name and no flavor was given.
    """
    matches = [
        plugin
        for plugin in m.plugins
        if plugin.qualified_name == qualified_name and (flavor is None or plugin.flavor == flavor)
    ]
    if not matches:
        return None
    if flavor is None and len(matches) > 1:
        raise plugins.PluginAmbiguousFlavorError(qualified_name, sorted(p.flavor for p in matches))
    return matches[0]


# --------------------------------------------------------------------------- #
# Security gate: bundled executable-surface extractor (dir plugins)
# --------------------------------------------------------------------------- #
def _collect_commands(value: object, label: str, out: list[str]) -> None:
    """Recursively collect ``command`` strings from a hooks/MCP config fragment."""
    if isinstance(value, dict):
        cmd = value.get("command")
        if isinstance(cmd, str) and cmd.strip():
            args = value.get("args")
            suffix = (
                " " + " ".join(a for a in args if isinstance(a, str))
                if isinstance(args, list)
                else ""
            )
            out.append(f"{label}: {cmd}{suffix}")
        for v in value.values():
            _collect_commands(v, label, out)
    elif isinstance(value, list):
        for v in value:
            _collect_commands(v, label, out)


def _surface_executable_surface(snap: Path, qualified_name: str) -> None:
    """Surface a plugin dir's executable surface (hooks, MCP/LSP launchers) for review.

    Text injection scanning alone is theater for an artifact bundling executable
    surface. Parses the bundled ``plugin.json`` / ``hooks/hooks.json`` /
    ``.mcp.json`` and records every shell hook command and MCP/LSP launcher to the
    warning buffer. Best-effort: malformed JSON is ignored (the file still vendors).
    """
    findings: list[str] = []
    candidates: list[tuple[Path, tuple[str, ...] | None]] = [
        (snap / ".claude-plugin" / "plugin.json", ("hooks", "mcpServers", "lspServers")),
        (snap / "hooks" / "hooks.json", None),
        (snap / ".mcp.json", ("mcpServers",)),
    ]
    for path, keys in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if keys is None:
            _collect_commands(data, "hook", findings)
        elif isinstance(data, dict):
            for key in keys:
                if key in data:
                    label = "hook" if key == "hooks" else "server"
                    _collect_commands(data[key], label, findings)
    if findings:
        unique = sorted(set(findings))
        _install_warnings.append(
            f"{qualified_name}: review bundled executable surface before enabling:\n  "
            + "\n  ".join(unique)
        )


# --------------------------------------------------------------------------- #
# Core vendoring (generic over kind.source_unit)
# --------------------------------------------------------------------------- #
def _deploy(
    project_root: Path,
    kind: plugin_kinds.PluginKind,
    *,
    repo_alias: str,
    plugin_name: str,
    source_path: str,
    version: SkillVersion,
    qualified_name: str,
    override_risk: bool,
) -> tuple[str, Path]:
    """Snapshot, gate, and vendor a plugin at the locked version. Returns (hash, target)."""
    rel = kind.vendor_target(
        repo_alias=repo_alias, plugin_name=plugin_name, source_path=source_path
    )
    target = paths.safe_project_path(project_root, rel)
    if target is None:
        raise install.ManifestPathEscapeError(f"plugin vendor path escapes project root: {rel!r}")

    pol = policy.effective_policy(project_root)
    policy.assert_repo_allowed(pol, repo_alias, install._repo_url(repo_alias))
    policy.assert_artifact_allowed(pol, "plugin", qualified_name)

    if kind.source_unit == "dir":
        snap = install._ensure_snapshot(repo_alias, version.sha, source_path, plugin_name)
        install._ensure_symlinks_safe(snap)
        hidden = content_guard.scan_directory(snap)
        if hidden:
            raise content_guard.HiddenUnicodeError(
                f"{qualified_name}: hidden Unicode found in plugin files:\n" + "\n".join(hidden)
            )
        _surface_executable_surface(snap, qualified_name)
        if pol.risk.classifier or pol.risk.llm_judge:
            risk.gate(
                install._gather_skill_text(snap),
                qualified_name=qualified_name,
                pol=pol,
                override_risk=override_risk,
            )
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            snap, target, symlinks=True, ignore=shutil.ignore_patterns(install._SNAPSHOT_SENTINEL)
        )
        return hashing.hash_tree(target), target

    # source_unit == "file"
    repo_dir = repos.clone_dir(repo_alias)
    try:
        content = git.get_backend().cat_file(repo_dir, version.sha, source_path)
    except git.GitError as exc:
        raise install.RollbackUnavailableError(
            f"could not read {repo_alias}/{plugin_name}@{version.sha[:12]}: {exc}"
        ) from exc
    content_guard.assert_no_hidden_unicode(content, source=qualified_name)
    if pol.risk.classifier or pol.risk.llm_judge:
        risk.gate(content, qualified_name=qualified_name, pol=pol, override_risk=override_risk)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return hashing.hash_text(content), target


def reconcile_registration(project_root: Path, m: Manifest) -> None:
    """Reconcile every loaded kind's client config from the installed set (idempotent).

    Used by `sync` after vendoring, and by install/update/rollback. Each kind
    reconciles only its own plugins (claude → settings.json + marketplace.json;
    file-drop kinds → no-op).
    """
    for kind in plugin_kinds.load_kinds(project_root).values():
        kind.register(project_root, m)


def _check_local_edits(project_root: Path, existing: InstalledPlugin, *, force: bool) -> None:
    """Refuse to overwrite a vendored plugin the user hand-edited since install."""
    if force or existing.content_hash is None:
        return
    target = paths.safe_project_path(project_root, existing.target_dir)
    if target is None or not target.exists():
        return
    current = (
        hashing.hash_tree(target)
        if target.is_dir()
        else hashing.hash_text(target.read_text(encoding="utf-8"))
    )
    if current != existing.content_hash:
        raise install.LocalEditsError(
            f"{existing.qualified_name}: files in {target} have been modified since install. "
            "Pass force=True (`--force`) to overwrite."
        )


def install_plugin(
    project_root: Path,
    qualified_name: str,
    *,
    flavor: str | None = None,
    track: str | None = None,
    pin: str | None = None,
    override_risk: bool = False,
) -> InstalledPlugin:
    """Vendor a plugin at the resolved version and record it in the manifest."""
    # Resolve so vendored target paths (via safe_project_path, which resolves)
    # stay relative to the same base — guards the macOS /var symlink.
    project_root = project_root.resolve()
    row = plugins.index_row(qualified_name, flavor)
    kind = _kind_for(row.flavor, project_root)
    try:
        version = install.resolve_install_version(
            row.repo_alias, row.source_path, track=track, pin=pin, artifact_name="plugin.json"
        )
    except git.GitError as exc:
        if pin is not None:
            raise PluginPinError(
                f"--pin {pin!r} is not a git ref in repo {row.repo_alias!r}; pin a tag or a "
                "short SHA from `aim plugin list` (the 'version' column is an upstream label, "
                "not a git ref)"
            ) from exc
        raise
    content_hash, target = _deploy(
        project_root,
        kind,
        repo_alias=row.repo_alias,
        plugin_name=row.plugin_name,
        source_path=row.source_path,
        version=version,
        qualified_name=qualified_name,
        override_risk=override_risk,
    )
    marketplace_name = row.repo_alias if getattr(kind, "uses_marketplace", False) else None

    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name, row.flavor)
    target_rel = str(target.relative_to(project_root))
    if existing is None:
        result = InstalledPlugin(
            qualified_name=qualified_name,
            repo_alias=row.repo_alias,
            repo_url=repos.get(row.repo_alias).url,
            flavor=row.flavor,
            source_path=row.source_path,
            target_dir=target_rel,
            marketplace_name=marketplace_name,
            current=version,
            content_hash=content_hash,
            pin=pin,
            track=track,
            risk_acknowledged=override_risk,
        )
        m.plugins.append(result)
    else:
        existing.push_history(version)
        existing.repo_alias = row.repo_alias
        existing.flavor = row.flavor
        existing.source_path = row.source_path
        existing.target_dir = target_rel
        existing.marketplace_name = marketplace_name
        existing.content_hash = content_hash
        if override_risk:
            existing.risk_acknowledged = True
        if pin is not None:
            existing.pin = pin
        if track is not None:
            existing.track = track
        result = existing
    manifest.save(project_root, m)
    kind.register(project_root, m)
    declarations._update_plugin(project_root, result)
    return result


def update(
    project_root: Path,
    qualified_name: str,
    *,
    flavor: str | None = None,
    force: bool = False,
    override_risk: bool = False,
) -> InstalledPlugin:
    """Update an installed plugin to the latest resolved version."""
    project_root = project_root.resolve()
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name, flavor)
    if existing is None:
        raise PluginNotInstalledError(qualified_name)
    row = plugins.index_row(qualified_name, existing.flavor)
    if row.source_path != existing.source_path:
        raise PluginSourcePathChangedError(
            f"{qualified_name}: source path moved from {existing.source_path!r} (installed) to "
            f"{row.source_path!r} (upstream). Reinstall explicitly to accept the move."
        )
    kind = _kind_for(existing.flavor, project_root)
    new_version = install.resolve_install_version(
        existing.repo_alias,
        existing.source_path,
        track=existing.track,
        pin=existing.pin,
        artifact_name="plugin.json",
    )
    _check_local_edits(project_root, existing, force=force)
    if new_version.sha == existing.current.sha:
        return existing
    content_hash, target = _deploy(
        project_root,
        kind,
        repo_alias=existing.repo_alias,
        plugin_name=row.plugin_name,
        source_path=existing.source_path,
        version=new_version,
        qualified_name=qualified_name,
        override_risk=override_risk,
    )
    existing.push_history(new_version)
    existing.content_hash = content_hash
    if override_risk:
        existing.risk_acknowledged = True
    existing.target_dir = str(target.relative_to(project_root))
    manifest.save(project_root, m)
    kind.register(project_root, m)
    declarations._update_plugin(project_root, existing)
    return existing


def update_many(
    project_root: Path,
    *,
    repo_alias: str | None = None,
    force: bool = False,
    override_risk: bool = False,
) -> list[install.BulkUpdateOutcome]:
    """Update all (or a repo-filtered subset of) installed plugins in a project."""
    m = _load_manifest(project_root)
    outcomes: list[install.BulkUpdateOutcome] = []
    for plugin in list(m.plugins):
        if repo_alias is not None and plugin.repo_alias != repo_alias:
            outcomes.append(
                install.BulkUpdateOutcome(plugin.qualified_name, "skipped", "repo filter")
            )
            continue
        try:
            result = update(
                project_root,
                plugin.qualified_name,
                flavor=plugin.flavor,
                force=force,
                override_risk=override_risk,
            )
            outcomes.append(
                install.BulkUpdateOutcome(
                    plugin.qualified_name, "updated", result.current.identifier()
                )
            )
        except (
            plugins.PluginNotIndexedError,
            PluginFlavorUnsupportedError,
            PluginSourcePathChangedError,
            install.LocalEditsError,
            git.GitError,
            install.RollbackUnavailableError,
        ) as exc:
            outcomes.append(install.BulkUpdateOutcome(plugin.qualified_name, "error", str(exc)))
    return outcomes


def _remove_vendored(project_root: Path, existing: InstalledPlugin) -> None:
    """Delete a plugin's vendored files (a directory or a single file)."""
    target = paths.safe_project_path(project_root, existing.target_dir)
    if target is None or not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def delete(project_root: Path, qualified_name: str, flavor: str | None = None) -> None:
    """Remove an installed plugin's vendored files, registration, and manifest entry."""
    project_root = project_root.resolve()
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name, flavor)
    if existing is None:
        raise PluginNotInstalledError(qualified_name)
    _remove_vendored(project_root, existing)
    m.plugins = [
        p
        for p in m.plugins
        if not (p.qualified_name == qualified_name and p.flavor == existing.flavor)
    ]
    manifest.save(project_root, m)
    # Unregister via the kind (config cleanup), if its spec is still loaded.
    kind = plugin_kinds.get_kind(existing.flavor, project_root)
    if kind is not None:
        kind.unregister(project_root, existing, m)
    declarations._remove_plugin(project_root, qualified_name, existing.flavor)


def rollback(
    project_root: Path,
    qualified_name: str,
    *,
    flavor: str | None = None,
    force: bool = False,
) -> InstalledPlugin:
    """Restore ``history[0]`` as the new current version."""
    project_root = project_root.resolve()
    m = _load_manifest(project_root)
    existing = _find_installed(m, qualified_name, flavor)
    if existing is None:
        raise PluginNotInstalledError(qualified_name)
    if not existing.history:
        raise install.NoHistoryToRollbackError(qualified_name)
    kind = _kind_for(existing.flavor, project_root)
    _check_local_edits(project_root, existing, force=force)
    target_version = existing.history[0]
    plugin_name = qualified_name.split("/", 1)[1]
    content_hash, target = _deploy(
        project_root,
        kind,
        repo_alias=existing.repo_alias,
        plugin_name=plugin_name,
        source_path=existing.source_path,
        version=target_version,
        qualified_name=qualified_name,
        override_risk=False,
    )
    existing.push_history(
        SkillVersion(tag=target_version.tag, sha=target_version.sha, installed_at=datetime.now(UTC))
    )
    existing.content_hash = content_hash
    existing.target_dir = str(target.relative_to(project_root))
    manifest.save(project_root, m)
    kind.register(project_root, m)
    declarations._update_plugin(project_root, existing)
    return existing
