"""Pluggable plugin *kinds*.

A "kind" owns two client-specific things: how to **discover** plugins in a repo,
and how to **register** them in a project. Everything else — clone, ref→SHA,
vendor the exact bytes, content-hash, security-scan, lockfile — stays in core
(`plugin_install`) so the package-manager guarantees hold no matter who wrote
the kind.

Two tiers:

- **Built-in kinds** ship with aim and may be code (``ClaudeKind``). They are
  trusted because they are part of the package.
- **External kinds** are declarative TOML specs dropped into a kinds dir
  (``<config>/kinds/*.toml`` globally, or ``<project>/.aim/kinds/*.toml``).
  They are data, never executed code, so a repo/teammate can ship one safely.
  Adding a new client (the opencode showcase) is one such file — no aim source
  change.

The registry loads built-ins first, then external specs (project overrides
global overrides built-in by ``name``).
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

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

    def discover(self, repo_alias: str, repo_dir: Path, sha: str, tree: list[str]) -> KindDiscovery:
        """Find this kind's plugins in a repo tree (paths from ``git ls-tree``)."""
        ...

    def vendor_target(self, *, repo_alias: str, plugin_name: str, source_path: str) -> str:
        """Repo-root-relative path where core vendors this plugin's bytes."""
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
    """Resolve a claude plugin entry's local ``source`` to a repo-relative path."""
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
    full = f"{root}/{rel}" if root else rel
    if not validation.is_safe_repo_path(full) or not full:
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
    try:
        data = json.loads(
            backend.cat_file(repo_dir, sha, f"{source_path}/.claude-plugin/plugin.json")
        )
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


def _repo_id_for_alias(repo_alias: str) -> str:
    """Resolve a registered repo alias to its source-agnostic identity token."""
    from aim.core import repos

    return policy.repo_id_for_url(repos.get(repo_alias).url)


class ClaudeKind:
    """Built-in kind for Claude Code marketplace plugins."""

    name = "claude"
    source_unit = "dir"
    uses_marketplace = True

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

    def _write_marketplace(self, project_root: Path, repo_id: str, names: list[str]) -> None:
        mkt_dir = self._marketplace_dir(project_root, repo_id)
        doc = {
            "name": _aim_marketplace_name(repo_id),
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
        for repo_id, names in by_repo.items():
            mkt_name = _aim_marketplace_name(repo_id)
            self._write_marketplace(project_root, repo_id, names)
            settings_json.register(
                project_root,
                settings_file=self._SETTINGS_FILE,
                marketplace_name=mkt_name,
                marketplace_path=f"{self._PLUGINS_DIR}/{mkt_name}",
                plugin_names=names,
            )

    def unregister(self, project_root: Path, installed: InstalledPlugin, m: Manifest) -> None:
        name = installed.qualified_name.split("/", 1)[1]
        repo_id = policy.repo_id_for_url(installed.repo_url)
        settings_json.unregister(
            project_root,
            settings_file=self._SETTINGS_FILE,
            marketplace_name=_aim_marketplace_name(repo_id),
            plugin_name=name,
        )
        remaining = self._claude_plugins_for_id(project_root, m, repo_id)
        if remaining:
            self._write_marketplace(project_root, repo_id, remaining)
        else:
            import shutil

            mkt_dir = self._marketplace_dir(project_root, repo_id)
            if mkt_dir.exists():
                shutil.rmtree(mkt_dir)


# --------------------------------------------------------------------------- #
# External: declarative kinds loaded from TOML
# --------------------------------------------------------------------------- #
class DiscoverSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: list[str]  # globs, repo-relative
    name_from: str = "stem"  # "stem" (file mode) — only mode supported today


class ConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    format: str = "json"
    set: dict[str, object] = Field(default_factory=dict)  # keypath-template -> value-template


class RegisterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_into: str  # literal path template, e.g. ".opencode/plugins/{name}.{ext}"
    vendor_as: str = "file"  # "file" | "dir"
    config: list[ConfigSpec] = Field(default_factory=list)


class KindSpec(BaseModel):
    """A declarative, distributable plugin kind (TOML)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    discover: DiscoverSpec
    registration: RegisterSpec = Field(alias="register")


def _ctx(*, repo_alias: str, plugin_name: str, source_path: str) -> dict:
    """Placeholder context for a kind's path/config templates: per-plugin tokens only.

    Deliberately closed to ``{repo}``/``{name}``/``{ext}`` — a kind owns its own
    paths (literal), so no layout-profile fields leak into the template engine.
    """
    ext = source_path.rsplit(".", 1)[-1] if "." in source_path else ""
    return {"repo": repo_alias, "name": plugin_name, "ext": ext}


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
    """A kind defined entirely by a TOML spec (no code). The opencode showcase."""

    uses_marketplace = False

    def __init__(self, spec: KindSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.source_unit = spec.registration.vendor_as

    def discover(self, repo_alias: str, repo_dir: Path, sha: str, tree: list[str]) -> KindDiscovery:
        out = KindDiscovery()
        for p in tree:
            if not validation.is_safe_repo_path(p):
                continue
            if not any(fnmatch.fnmatch(p, g) for g in self.spec.discover.manifest):
                continue
            stem = p.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if not validation.is_valid_plugin_name(stem):
                continue
            out.plugins.append(
                DiscoveredPlugin(
                    name=stem, kind=self.name, source_path=p, source_unit=self.source_unit
                )
            )
        return out

    def vendor_target(self, *, repo_alias: str, plugin_name: str, source_path: str) -> str:
        ctx = _ctx(repo_alias=repo_alias, plugin_name=plugin_name, source_path=source_path)
        return _render(self.spec.registration.vendor_into, ctx)

    def register(self, project_root: Path, m: Manifest) -> None:
        for plugin in m.plugins:
            if plugin.flavor == self.name and _is_vendored(project_root, plugin):
                self._apply(project_root, plugin, remove=False)

    def unregister(self, project_root: Path, installed: InstalledPlugin, m: Manifest) -> None:
        self._apply(project_root, installed, remove=True)

    def _apply(self, project_root: Path, plugin: InstalledPlugin, *, remove: bool) -> None:
        name = plugin.qualified_name.split("/", 1)[1]
        ctx = _ctx(repo_alias=plugin.repo_alias, plugin_name=name, source_path=plugin.source_path)
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


def _kinds_dirs(project_root: Path | None) -> list[Path]:
    dirs = [paths.user_config_dir() / "kinds"]
    if project_root is not None:
        dirs.append(paths.project_aim_dir(project_root) / "kinds")
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
            except Exception:
                continue  # a malformed external kind is ignored, never fatal
            kinds[spec.name] = DeclarativeKind(spec)
    return kinds


def get_kind(name: str, project_root: Path | None = None) -> PluginKind | None:
    """Return the kind named ``name``, or None if no spec is loaded for it."""
    return load_kinds(project_root).get(name)
