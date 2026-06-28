"""Pluggable plugin *kinds*.

A "kind" owns two client-specific things: how to **discover** plugins in a repo,
and how to **register** them in a project. Everything else — clone, ref→SHA,
vendor the exact bytes, content-hash, security-scan, lockfile — stays in core
(`plugin_install`) so the package-manager guarantees hold no matter who wrote
the kind.

Two tiers:

- **Built-in kinds** ship with aim and may be code (``ClaudeKind``). They are
  trusted because they are part of the package.
- **External kinds** are declarative TOML specs dropped into a targets dir
  (``<config>/targets/*.toml`` globally, or ``<project>/.aim/targets/*.toml``).
  They are data, never executed code, so a repo/teammate can ship one safely.
  Adding a new client (the opencode showcase) is one such file — no aim source
  change.

The registry loads built-ins first, then external specs (project overrides
global overrides built-in by ``name``).
"""

from __future__ import annotations

import json
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aim.core import git, paths, policy, settings_json, validation
from aim.core.models import InstalledPlugin, Manifest


# --------------------------------------------------------------------------- #
# Normalized discovery records (kind-agnostic; core indexes these)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DiscoveredPlugin:
    """A plugin found by a kind. ``source_unit`` tells core what bytes to vendor."""

    name: str
    kind: str
    source_path: str
    source_unit: str  # "dir" | "file" — the bytes core snapshots/hashes/vendors
    marketplace_name: str | None = None
    version: str | None = None
    description: str | None = None
    category: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveredMarketplace:
    """A marketplace catalog found by a kind (claude only, today)."""

    name: str
    manifest_path: str
    owner_name: str | None = None
    owner_url: str | None = None
    title: str | None = None
    description: str | None = None


@dataclass
class KindDiscovery:
    """What a kind found in one repo."""

    plugins: list[DiscoveredPlugin] = field(default_factory=list)
    marketplaces: list[DiscoveredMarketplace] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PluginKind(Protocol):
    """The contract every kind (built-in or declarative) satisfies."""

    name: str
    source_unit: str  # "dir" | "file"
    uses_marketplace: bool  # whether installs record an aim-local marketplace name
    manifest_filename: str  # manifest path (rel. to source dir) for version tracking / tag honesty

    def discover(self, repo_alias: str, repo_dir: Path, sha: str, tree: list[str]) -> KindDiscovery:
        """Find this kind's plugins in a repo tree (paths from ``git ls-tree``)."""
        ...

    def vendor_target(self, *, repo_alias: str, plugin_name: str, source_path: str) -> str:
        """Repo-root-relative path where core vendors this plugin's bytes."""
        ...

    def executable_surface(self, snap: Path) -> list[str]:
        """Human-readable lines describing the executable surface bundled in a vendored
        plugin (hook commands, MCP/LSP launchers) for the installer to surface for review.
        Empty when the kind bundles none."""
        ...

    def register(self, project_root: Path, m: Manifest) -> None:
        """Reconcile client config (e.g. settings.json) from the installed set. Idempotent."""
        ...

    def unregister(self, project_root: Path, installed: InstalledPlugin, m: Manifest) -> None:
        """Remove one plugin's client-config registration (``m`` already excludes it)."""
        ...


# --------------------------------------------------------------------------- #
# Built-in: Claude (marketplace.json discovery + settings.json registration)
# --------------------------------------------------------------------------- #
_MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"


def _marketplace_root(manifest_path: str) -> str:
    if manifest_path == _MARKETPLACE_MANIFEST:
        return ""
    suffix = "/" + _MARKETPLACE_MANIFEST
    return manifest_path[: -len(suffix)] if manifest_path.endswith(suffix) else ""


def _resolve_plugin_source(root: str, plugin_root: str, source: str) -> str | None:
    """Resolve a claude plugin entry's local ``source`` to a repo-relative path.

    Returns ``""`` when the source points at the marketplace root itself — a
    ``"./"`` / ``"."`` source for a marketplace at the repo root means the whole
    repo is the plugin (the common single-plugin-repo shape). Returns None when
    the source is absolute or escapes the repo.
    """
    if source.startswith(("/", "\\")):
        return None  # absolute → reject
    pr = plugin_root.strip()
    if pr.startswith("./"):
        pr = pr[2:]
    pr = pr.strip("/")
    if source.startswith("./"):
        rel = source[2:]
    elif pr:
        rel = f"{pr}/{source}"
    else:
        rel = source
    rel = rel.strip("/")
    if rel == ".":
        rel = ""
    full = "/".join(p for p in (root, rel) if p)
    if not validation.is_safe_repo_path(full):
        return None
    return full


def _coerce_owner(raw: object) -> tuple[str | None, str | None]:
    if isinstance(raw, dict):
        name = raw.get("name")
        url = raw.get("url")
        return (name if isinstance(name, str) else None, url if isinstance(url, str) else None)
    if isinstance(raw, str):
        return (raw, None)
    return (None, None)


def _plugin_json_version(repo_dir: Path, sha: str, source_path: str) -> str | None:
    """A claude plugin's own version from ``<source>/.claude-plugin/plugin.json``, if any.

    This is the plugin's self-declared version, which is the source of truth — the
    marketplace entry's ``version`` is only a fallback when the plugin.json lacks one.
    """
    backend = git.get_backend()
    rel = (
        f"{source_path}/.claude-plugin/plugin.json" if source_path else ".claude-plugin/plugin.json"
    )
    try:
        data = json.loads(backend.cat_file(repo_dir, sha, rel))
    except (git.GitError, json.JSONDecodeError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def _is_vendored(project_root: Path, plugin: InstalledPlugin) -> bool:
    """True when a plugin's vendored files are present on disk.

    ``register`` reconciles client config from this — a plugin that failed to
    vendor (e.g. a blocked risk gate on re-sync) must not get a marketplace or
    settings.json entry pointing at files that aren't there.
    """
    target = paths.safe_project_path(project_root, plugin.target_dir)
    return target is not None and target.exists()


def _aim_marketplace_name(repo_id: str) -> str:
    """Return the aim-local marketplace name for a repo identity token.

    Source-agnostic (``aim-<repo_id>``) so the committed `.claude/settings.json`
    enablement keys and vendor paths are portable across machines and clone-URL
    forms — these `.claude/` files are committed and are NOT alias-translated.
    """
    return f"aim-{repo_id}"


def _semantic_marketplace_label(upstream_name: str, repo_id: str) -> str:
    """Readable, globally-unique Claude-facing marketplace key: ``<name>-<short id>``.

    Leads with the upstream marketplace name (what Claude displays) but appends a
    short repo-id so it cannot collide with another marketplace of the same name
    in a different config scope — Claude's marketplace namespace is FLAT across
    global `~/.claude/settings.json` and project `.claude/settings.json`, and aim
    cannot dedupe against machine-local global config without breaking the
    determinism of the committed project file. Deterministic and alias-agnostic
    (depends only on the source-agnostic repo id), so committed files stay
    portable across machines and clone-URL forms.
    """
    return f"{upstream_name}-{repo_id[:8]}"


def _declared_marketplace_labels(project_root: Path) -> dict[str, str]:
    """Map repo_id -> the Claude-facing marketplace label (``<name>-<short id>``).

    Only the `.claude/settings.json` key and the synthetic `marketplace.json`
    ``name`` use this readable label; the vendor dir and the lock stay id-based
    (``aim-<repo_id>``) so they remain alias-independent. A repo is labelled only
    when its claude plugins share exactly one upstream name; otherwise the caller
    falls back to the id form.
    """
    from aim.core import declarations

    try:
        decl = declarations.load(project_root)
    except declarations.DeclarationsNotFoundError:
        return {}
    by_id: dict[str, set[str]] = {}
    for p in decl.plugins:
        if p.flavor != "claude" or not p.marketplace_name:
            continue
        url = decl.repos.get(p.repo_alias)
        if not url:
            continue
        rid = policy.repo_id_for_url(url)
        if p.marketplace_name != _aim_marketplace_name(rid):  # ignore stale id-form values
            by_id.setdefault(rid, set()).add(p.marketplace_name)
    # A repo earns a label only when its claude plugins share exactly one name.
    candidates = {
        rid: _semantic_marketplace_label(next(iter(names)), rid)
        for rid, names in by_id.items()
        if len(names) == 1
    }
    # Defensive: the short-id suffix makes intra-project collisions near-impossible,
    # but if two repos still produce the same label, drop both to the id form.
    used: dict[str, int] = {}
    for label in candidates.values():
        used[label] = used.get(label, 0) + 1
    return {rid: label for rid, label in candidates.items() if used[label] == 1}


def _repo_id_for_alias(repo_alias: str) -> str:
    """Resolve a registered repo alias to its source-agnostic identity token."""
    from aim.core import repos

    return policy.repo_id_for_url(repos.get(repo_alias).url)


def _get_keypath(doc: dict, dotted: str) -> object:
    """Walk a dotted keypath into a parsed JSON object; None if any segment is missing."""
    cur: object = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


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


class ClaudeKind:
    """Built-in kind for Claude Code marketplace plugins."""

    name = "claude"
    source_unit = "dir"
    uses_marketplace = True
    # Path RELATIVE to the plugin's source dir. For a whole-repo plugin
    # (source_path == "") this is what version-tracking follows, so it must be the
    # manifest's real nested location, not a bare top-level "plugin.json".
    manifest_filename = ".claude-plugin/plugin.json"

    def executable_surface(self, snap: Path) -> list[str]:
        """Parse a claude plugin dir's bundled plugin.json / hooks.json / .mcp.json and
        return every shell hook command and MCP/LSP launcher. Best-effort: malformed JSON
        is ignored (the file still vendors)."""
        candidates: list[tuple[Path, tuple[str, ...] | None]] = [
            (snap / ".claude-plugin" / "plugin.json", ("hooks", "mcpServers", "lspServers")),
            (snap / "hooks" / "hooks.json", None),
            (snap / ".mcp.json", ("mcpServers",)),
        ]
        findings: list[str] = []
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
        return findings

    def discover(self, repo_alias: str, repo_dir: Path, sha: str, tree: list[str]) -> KindDiscovery:
        out = KindDiscovery()
        backend = git.get_backend()
        seen: set[str] = set()
        for p in tree:
            if p != _MARKETPLACE_MANIFEST and not p.endswith("/" + _MARKETPLACE_MANIFEST):
                continue
            if not validation.is_safe_repo_path(p):
                continue
            try:
                data = json.loads(backend.cat_file(repo_dir, sha, p))
            except (git.GitError, json.JSONDecodeError) as exc:
                out.warnings.append(f"{repo_alias}: skipped marketplace {p}: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            name = data.get("name")
            if not isinstance(name, str) or not validation.is_valid_marketplace_name(name):
                out.warnings.append(f"{repo_alias}: marketplace {p}: invalid name {name!r}")
                continue
            if name in seen:
                out.warnings.append(f"{repo_alias}: duplicate marketplace {name!r} at {p}")
                continue
            seen.add(name)
            raw_meta = data.get("metadata")
            meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
            owner_name, owner_url = _coerce_owner(data.get("owner"))
            desc = data.get("description")
            if not isinstance(desc, str):
                md = meta.get("description")
                desc = md if isinstance(md, str) else None
            out.marketplaces.append(
                DiscoveredMarketplace(name, p, owner_name, owner_url, name, desc)
            )
            root = _marketplace_root(p)
            raw_pr = meta.get("pluginRoot")
            plugin_root = raw_pr if isinstance(raw_pr, str) else ""
            entries = data.get("plugins")
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                pname = entry.get("name")
                if not isinstance(pname, str) or not validation.is_valid_plugin_name(pname):
                    out.warnings.append(f"{repo_alias}: plugin in {name}: invalid name {pname!r}")
                    continue
                src = entry.get("source")
                if not isinstance(src, str):
                    out.warnings.append(f"{repo_alias}: plugin {pname}: non-local source skipped")
                    continue
                resolved = _resolve_plugin_source(root, plugin_root, src)
                if resolved is None:
                    out.warnings.append(f"{repo_alias}: plugin {pname}: unsafe source {src!r}")
                    continue
                kw = entry.get("keywords")
                out.plugins.append(
                    DiscoveredPlugin(
                        name=pname,
                        kind="claude",
                        source_path=resolved,
                        source_unit="dir",
                        marketplace_name=name,
                        version=_plugin_json_version(repo_dir, sha, resolved)
                        or (
                            entry.get("version") if isinstance(entry.get("version"), str) else None
                        ),
                        description=entry.get("description")
                        if isinstance(entry.get("description"), str)
                        else None,
                        category=entry.get("category")
                        if isinstance(entry.get("category"), str)
                        else None,
                        keywords=tuple(k for k in kw if isinstance(k, str))
                        if isinstance(kw, list)
                        else (),
                    )
                )
        return out

    # Claude's paths are the kind's own concern, not a layout-profile field:
    # settings.json is the client's fixed location; the vendor dir is aim's choice.
    _PLUGINS_DIR = ".claude/plugins"
    _SETTINGS_FILE = ".claude/settings.json"

    def _marketplace_dir(self, project_root: Path, repo_id: str) -> Path:
        rel = f"{self._PLUGINS_DIR}/{_aim_marketplace_name(repo_id)}"
        safe = paths.safe_project_path(project_root, rel)
        if safe is None:
            raise ValueError(f"plugins path escapes project root: {rel!r}")
        return safe

    def vendor_target(self, *, repo_alias: str, plugin_name: str, source_path: str) -> str:
        # Vendor under the id-based marketplace dir so the committed `.claude/`
        # tree is portable (alias-independent) across machines.
        repo_id = _repo_id_for_alias(repo_alias)
        return f"{self._PLUGINS_DIR}/{_aim_marketplace_name(repo_id)}/{plugin_name}"

    def _claude_plugins_for_id(self, project_root: Path, m: Manifest, repo_id: str) -> list[str]:
        return [
            p.qualified_name.split("/", 1)[1]
            for p in m.plugins
            if p.flavor == "claude"
            and policy.repo_id_for_url(p.repo_url) == repo_id
            and _is_vendored(project_root, p)
        ]

    def _write_marketplace(
        self, project_root: Path, repo_id: str, names: list[str], display_name: str | None = None
    ) -> None:
        mkt_dir = self._marketplace_dir(project_root, repo_id)
        doc = {
            "name": display_name or _aim_marketplace_name(repo_id),
            "owner": {"name": "aim"},
            "plugins": [{"name": n, "source": f"./{n}"} for n in names],
        }
        cp = mkt_dir / ".claude-plugin"
        cp.mkdir(parents=True, exist_ok=True)
        (cp / "marketplace.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def register(self, project_root: Path, m: Manifest) -> None:
        # Reconcile every claude repo's marketplace + settings.json from the
        # installed set — but only plugins actually vendored on disk, so a
        # failed/blocked vendor never leaves a marketplace entry pointing at nothing.
        # Group by source-agnostic repo id so the committed files are portable.
        by_repo: dict[str, list[str]] = {}
        for p in m.plugins:
            if p.flavor == "claude" and _is_vendored(project_root, p):
                repo_id = policy.repo_id_for_url(p.repo_url)
                by_repo.setdefault(repo_id, []).append(p.qualified_name.split("/", 1)[1])
        labels = _declared_marketplace_labels(project_root)
        desired: set[str] = set()
        for repo_id, names in by_repo.items():
            vendor_name = _aim_marketplace_name(repo_id)  # id-based vendor dir (alias-independent)
            mkt_name = labels.get(repo_id, vendor_name)  # Claude-facing key: readable when known
            desired.add(mkt_name)
            self._write_marketplace(project_root, repo_id, names, display_name=mkt_name)
            settings_json.register(
                project_root,
                settings_file=self._SETTINGS_FILE,
                marketplace_name=mkt_name,
                marketplace_path=f"{self._PLUGINS_DIR}/{vendor_name}",
                plugin_names=names,
            )
        # Retire stale aim-managed keys: an id-form key superseded by the semantic
        # name (upgrade), or a semantic name demoted to id-form on a new collision.
        settings_json.prune_marketplaces(
            project_root,
            settings_file=self._SETTINGS_FILE,
            keep=desired,
            path_prefix=f"{self._PLUGINS_DIR}/aim-",
        )

    def unregister(self, project_root: Path, installed: InstalledPlugin, m: Manifest) -> None:
        name = installed.qualified_name.split("/", 1)[1]
        repo_id = policy.repo_id_for_url(installed.repo_url)
        mkt_name = _declared_marketplace_labels(project_root).get(
            repo_id, _aim_marketplace_name(repo_id)
        )
        settings_json.unregister(
            project_root,
            settings_file=self._SETTINGS_FILE,
            marketplace_name=mkt_name,
            plugin_name=name,
        )
        remaining = self._claude_plugins_for_id(project_root, m, repo_id)
        if remaining:
            self._write_marketplace(project_root, repo_id, remaining, display_name=mkt_name)
        else:
            import shutil

            mkt_dir = self._marketplace_dir(project_root, repo_id)
            if mkt_dir.exists():
                shutil.rmtree(mkt_dir)
        # aim cleans the committed project surface, but Claude keeps machine-local
        # copies (registry + data dir) it does not garbage-collect. aim does not
        # own that state, so warn the user to purge it themselves.
        steps = [f"claude plugin uninstall {name}@{mkt_name}"]
        if not remaining:
            steps.append(f"claude plugin marketplace remove {mkt_name}")
        steps.append(f"rm -rf ~/.claude/plugins/data/{name}-{mkt_name}")
        _removal_warnings.append(
            f"{installed.qualified_name}: removed from the project. Claude keeps "
            "machine-local copies it will not auto-remove — purge them with:\n  "
            + "\n  ".join(steps)
        )


# --------------------------------------------------------------------------- #
# External: declarative kinds loaded from TOML
# --------------------------------------------------------------------------- #
def _reject_unsafe_rel_path(value: str, *, field: str) -> str:
    """Reject absolute or parent-escaping path templates in a kind spec.

    A kind controls where bytes get vendored and which client-config files get
    written. Shared kinds (vendored from a repo) have a wider blast radius than a
    file a teammate drops in their own ``.aim/targets``, so we clamp the spec at
    load time in addition to the install-time ``safe_project_path`` guard. The
    only interpolations are ``{name}``/``{repo}``, both validated to the alias
    charset (no ``/`` or ``..``), so checking the raw template is sufficient.

    Args:
        value: The path template (e.g. ``.opencode/plugins/{name}``).
        field: Human-readable field name, for the error message.

    Raises:
        ValueError: The template is absolute or contains a ``..`` segment.
    """
    if value.startswith(("/", "\\")):
        raise ValueError(f"{field} must be a relative path, got absolute: {value!r}")
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError(f"{field} must not contain '..' path segments: {value!r}")
    return value


class ManifestSpec(BaseModel):
    """How to find and read a plugin's self-describing metadata file.

    Discovery is anchored on this file: any directory in a repo that contains
    ``file`` is a plugin, and that whole directory is vendored. The plugin's name
    comes from the manifest's ``name`` keypath, so loose files without metadata are
    not discoverable.
    """

    model_config = ConfigDict(extra="forbid")
    file: str  # the manifest filename, e.g. "gemini-extension.json" or "package.json" (JSON)
    name: str = "name"  # dotted keypath in the manifest to the plugin name
    description: str | None = None  # optional dotted keypath to a description string


class ConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    format: str = "json"
    set: dict[str, object] = Field(default_factory=dict)  # keypath-template -> value-template

    @field_validator("file")
    @classmethod
    def _file_is_safe(cls, v: str) -> str:
        return _reject_unsafe_rel_path(v, field="register.config.file")


class RegisterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_into: str  # literal path template, e.g. ".gemini/extensions/{name}"
    config: list[ConfigSpec] = Field(default_factory=list)

    @field_validator("vendor_into")
    @classmethod
    def _vendor_into_is_safe(cls, v: str) -> str:
        return _reject_unsafe_rel_path(v, field="register.vendor_into")


class KindSpec(BaseModel):
    """A declarative, distributable plugin kind (TOML)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    manifest: ManifestSpec
    registration: RegisterSpec = Field(alias="register")


def _ctx(*, repo_alias: str, plugin_name: str) -> dict:
    """Placeholder context for a kind's path/config templates: per-plugin tokens only.

    Deliberately closed to ``{repo}``/``{name}`` — a kind owns its own paths
    (literal), so no layout-profile fields leak into the template engine.
    """
    return {"repo": repo_alias, "name": plugin_name}


def _render(template: str, ctx: dict) -> str:
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _render_value(value: object, ctx: dict) -> object:
    if isinstance(value, str):
        return _render(value, ctx)
    if isinstance(value, dict):
        return {k: _render_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v, ctx) for v in value]
    return value


def _set_key(doc: dict, dotted: str, value: object) -> None:
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _del_key(doc: dict, dotted: str) -> None:
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    cur.pop(parts[-1], None)


class DeclarativeKind:
    """A kind defined entirely by a TOML spec (no code).

    A plugin is a directory containing the kind's manifest file; discovery reads
    the plugin's name from that manifest and vendors the whole directory.
    """

    uses_marketplace = False
    source_unit = "dir"  # the plugin's bytes are always its enclosing directory

    def __init__(self, spec: KindSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.manifest_filename = spec.manifest.file

    def discover(self, repo_alias: str, repo_dir: Path, sha: str, tree: list[str]) -> KindDiscovery:
        out = KindDiscovery()
        fname = self.spec.manifest.file
        suffix = "/" + fname
        seen: set[str] = set()
        for p in tree:
            if not validation.is_safe_repo_path(p):
                continue
            if p == fname:
                source_path = ""  # manifest at repo root → the repo itself is the plugin
            elif p.endswith(suffix):
                source_path = p[: -len(suffix)]
            else:
                continue
            if source_path in seen:
                continue
            data = self._read_manifest(repo_dir, sha, p, repo_alias, out)
            if data is None:
                continue
            name = _get_keypath(data, self.spec.manifest.name)
            if not isinstance(name, str):
                continue
            if not validation.is_valid_plugin_name(name):
                out.warnings.append(f"{repo_alias}: {p}: invalid plugin name {name!r}")
                continue
            seen.add(source_path)
            out.plugins.append(
                DiscoveredPlugin(
                    name=name,
                    kind=self.name,
                    source_path=source_path,
                    source_unit=self.source_unit,
                    description=self._read_description(data),
                )
            )
        return out

    def _read_manifest(
        self, repo_dir: Path, sha: str, manifest_path: str, repo_alias: str, out: KindDiscovery
    ) -> dict | None:
        """Parse a plugin's JSON manifest, or None when it is unreadable or not an object."""
        try:
            data = json.loads(git.get_backend().cat_file(repo_dir, sha, manifest_path))
        except (git.GitError, json.JSONDecodeError) as exc:
            out.warnings.append(f"{repo_alias}: skipped manifest {manifest_path}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def _read_description(self, data: dict) -> str | None:
        """Read the description keypath when the kind declares one; else None."""
        keypath = self.spec.manifest.description
        if not keypath:
            return None
        value = _get_keypath(data, keypath)
        return value if isinstance(value, str) else None

    def executable_surface(self, snap: Path) -> list[str]:
        """Surface every shell launcher (``{command, args}``) declared anywhere in the
        vendored manifest, for the installer to flag for review. Best-effort: malformed
        JSON is ignored (the file still vendors)."""
        path = snap / self.spec.manifest.file
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        findings: list[str] = []
        _collect_commands(data, "command", findings)
        return findings

    def vendor_target(self, *, repo_alias: str, plugin_name: str, source_path: str) -> str:
        ctx = _ctx(repo_alias=repo_alias, plugin_name=plugin_name)
        return _render(self.spec.registration.vendor_into, ctx)

    def register(self, project_root: Path, m: Manifest) -> None:
        for plugin in m.plugins:
            if plugin.flavor == self.name and _is_vendored(project_root, plugin):
                self._apply(project_root, plugin, remove=False)

    def unregister(self, project_root: Path, installed: InstalledPlugin, m: Manifest) -> None:
        self._apply(project_root, installed, remove=True)

    def _apply(self, project_root: Path, plugin: InstalledPlugin, *, remove: bool) -> None:
        name = plugin.qualified_name.split("/", 1)[1]
        ctx = _ctx(repo_alias=plugin.repo_alias, plugin_name=name)
        for cfg in self.spec.registration.config:
            if cfg.format != "json":
                continue  # json first; yaml/toml are easy follow-ons
            path = paths.safe_project_path(project_root, _render(cfg.file, ctx))
            if path is None:
                continue
            data = {}
            if path.exists() and path.read_text(encoding="utf-8").strip():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            for key_tmpl, val_tmpl in cfg.set.items():
                key = _render(key_tmpl, ctx)
                if remove:
                    _del_key(data, key)
                else:
                    _set_key(data, key, _render_value(val_tmpl, ctx))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_BUILTINS: list[PluginKind] = [ClaudeKind()]

# Malformed/stale external kind specs are skipped (never fatal), but recorded here
# so callers can tell the user WHICH `.toml` was ignored and why — silently dropping
# a kind looks like "my plugin disappeared". Thread-safe for concurrent discovery.
_load_warnings: list[str] = []
_load_lock = threading.Lock()


def take_load_warnings() -> list[str]:
    """Return and clear warnings about external kind specs ignored during loading."""
    with _load_lock:
        out = list(_load_warnings)
        _load_warnings.clear()
    return out


# Hints a kind emits when a plugin is uninstalled, for residue aim cannot reach
# (e.g. Claude's machine-local registry, which aim does not own and Claude does
# not garbage-collect). Drained by `plugin_install.delete` into its warning channel.
_removal_warnings: list[str] = []


def take_removal_warnings() -> list[str]:
    """Return and clear post-uninstall cleanup hints emitted by kinds."""
    out = list(_removal_warnings)
    _removal_warnings.clear()
    return out


def _kinds_dirs(project_root: Path | None) -> list[Path]:
    dirs = [paths.user_config_dir() / "targets"]
    if project_root is not None:
        dirs.append(paths.project_aim_dir(project_root) / "targets")
    return dirs


def load_kinds(project_root: Path | None = None) -> dict[str, PluginKind]:
    """Return all available kinds by name: built-ins, then external TOML specs.

    External specs override built-ins, and project specs override global ones, by
    ``name`` (last writer wins in built-in → global → project order).
    """
    kinds: dict[str, PluginKind] = {k.name: k for k in _BUILTINS}
    for d in _kinds_dirs(project_root):
        if not d.is_dir():
            continue
        for toml_path in sorted(d.glob("*.toml")):
            try:
                spec = KindSpec.model_validate(tomllib.loads(toml_path.read_text(encoding="utf-8")))
            except Exception as exc:
                # Ignored, never fatal — but recorded so the user can find the bad spec.
                with _load_lock:
                    _load_warnings.append(f"{toml_path}: ignored invalid target spec: {exc}")
                continue
            kinds[spec.name] = DeclarativeKind(spec)
    return kinds


def get_kind(name: str, project_root: Path | None = None) -> PluginKind | None:
    """Return the kind named ``name``, or None if no spec is loaded for it."""
    return load_kinds(project_root).get(name)
