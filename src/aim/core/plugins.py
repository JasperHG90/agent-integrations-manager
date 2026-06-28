"""Plugin and marketplace discovery from registered source repos.

Discovery is driven by the **plugin kind registry** (`plugin_kinds`): each kind
knows what to look for. Built-in kinds (claude) plus any external declarative
kinds (TOML in the global targets dir) are all consulted. Results are persisted in
``MarketplaceIndex`` + ``PluginIndex`` and queried by `aim plugin list`.

Duplicate plugin names within a repo are deduplicated by precedence (built-in
kinds first, then by path depth); shadowed duplicates are dropped with a warning
so `aim repo add`/`refresh` can surface them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import delete, select

from aim.core import db, git, plugin_kinds, repos
from aim.core.models import MarketplaceIndex, PluginIndex
from aim.core.plugin_kinds import DiscoveredMarketplace, DiscoveredPlugin

# Items that look valid but fail to parse are dropped from the index; reasons are
# collected so `aim repo add`/`refresh` can warn. Thread-safe for `refresh_many`.
_skip_warnings: list[str] = []
_skip_lock = threading.Lock()


def _warn_skip(message: str) -> None:
    """Record a warning about a plugin/marketplace skipped during discovery."""
    with _skip_lock:
        _skip_warnings.append(message)


def take_skipped_warnings() -> list[str]:
    """Return and clear warnings about items skipped during discovery."""
    with _skip_lock:
        out = list(_skip_warnings)
        _skip_warnings.clear()
    return out


class PluginNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the plugin index."""


class PluginAmbiguousFlavorError(KeyError):
    """A plugin name resolves to multiple kinds; the caller must pass a flavor."""

    def __init__(self, qualified_name: str, flavors: list[str]) -> None:
        self.qualified_name = qualified_name
        self.flavors = flavors
        super().__init__(qualified_name)

    def __str__(self) -> str:
        return (
            f"{self.qualified_name} is ambiguous across targets: {', '.join(self.flavors)}; "
            "pass --target"
        )


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering plugins/marketplaces in a repo at a resolved SHA."""

    repo_alias: str
    sha: str
    marketplaces: list[DiscoveredMarketplace]
    plugins: list[DiscoveredPlugin]
    shadowed: list[DiscoveredPlugin] = field(default_factory=list)


def _rank(plugin: DiscoveredPlugin, kind_order: dict[str, int]) -> tuple[int, int, str]:
    """Precedence for deduplicating same-named plugins (lower wins)."""
    return (kind_order.get(plugin.kind, 99), plugin.source_path.count("/"), plugin.source_path)


def owned_dir_prefixes(repo_alias: str, repo_dir: Path, sha: str, tree: list[str]) -> set[str]:
    """Source dirs of dir-kind plugins in a repo.

    Skills/agents/rules living UNDER one of these are bundled with a plugin and
    must not surface as standalone artifacts (filter with `is_plugin_owned`).
    """
    prefixes: set[str] = set()
    for kind in plugin_kinds.load_kinds().values():
        for plugin in kind.discover(repo_alias, repo_dir, sha, tree).plugins:
            if plugin.source_unit == "dir":
                prefixes.add(plugin.source_path.rstrip("/"))
    return prefixes


def is_plugin_owned(path: str, prefixes: set[str]) -> bool:
    """True if a repo-relative path lives inside one of the plugin source dirs."""
    return any(path == d or path.startswith(f"{d}/") for d in prefixes)


def _discover_in_repo(repo_alias: str, kinds: dict[str, plugin_kinds.PluginKind]) -> IndexResult:
    """Run the given kinds' discovery over a repo's tree once, deduped by (name, kind).

    Args:
        repo_alias: Alias of the registered source repo to scan.
        kinds: The kind registry to apply (built-in + whichever external specs).

    Returns:
        An IndexResult with marketplaces, the winning plugins, and shadowed dupes.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    backend = git.get_backend()
    sha = backend.resolve_ref(repo_dir, repo.default_ref)
    tree = backend.ls_tree(repo_dir, sha)

    kind_order = {name: i for i, name in enumerate(kinds)}

    marketplaces: list[DiscoveredMarketplace] = []
    candidates: list[DiscoveredPlugin] = []
    for kind in kinds.values():
        found = kind.discover(repo_alias, repo_dir, sha, tree)
        marketplaces.extend(found.marketplaces)
        candidates.extend(found.plugins)
        for warning in found.warnings:
            _warn_skip(warning)

    # Deduplicate by (name, kind): the same name under DIFFERENT kinds coexists
    # (the index PK is qualified_name + flavor). Only same-name-same-kind collides,
    # broken by path precedence.
    by_id: dict[tuple[str, str], list[DiscoveredPlugin]] = {}
    for plugin in candidates:
        by_id.setdefault((plugin.name, plugin.kind), []).append(plugin)
    indexed: list[DiscoveredPlugin] = []
    shadowed: list[DiscoveredPlugin] = []
    for _, group in sorted(by_id.items()):
        group.sort(key=lambda p: _rank(p, kind_order))
        indexed.append(group[0])
        for dupe in group[1:]:
            shadowed.append(dupe)
            _warn_skip(
                f"{repo_alias}: plugin {dupe.name!r} ({dupe.kind}) shadowed ({dupe.source_path})"
            )

    return IndexResult(
        repo_alias=repo_alias,
        sha=sha,
        marketplaces=marketplaces,
        plugins=indexed,
        shadowed=shadowed,
    )


def discover(repo_alias: str, project_root: Path | None = None) -> IndexResult:
    """Discover plugins and marketplaces in a registered repo at its default ref.

    Every registered kind inspects the repo tree once: built-ins plus external global
    targets, and — when ``project_root`` is given — the project's own ``.aim/targets``
    specs too.

    Args:
        repo_alias: Alias of the registered source repo to scan.
        project_root: If given, also apply the project's ``.aim/targets`` specs.

    Returns:
        An IndexResult with marketplaces, the winning plugins, and shadowed dupes.
    """
    kinds = plugin_kinds.load_kinds(project_root)
    for warning in plugin_kinds.take_load_warnings():
        _warn_skip(warning)
    return _discover_in_repo(repo_alias, kinds)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover plugins/marketplaces in a repo and write index rows.

    Args:
        repo_alias: Alias of the registered source repo to index.

    Returns:
        The IndexResult produced by discovery.
    """
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(delete(PluginIndex).where(PluginIndex.repo_alias == repo_alias))  # type: ignore[arg-type]
        session.exec(
            delete(MarketplaceIndex).where(MarketplaceIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        )
        for mkt in result.marketplaces:
            session.add(
                MarketplaceIndex(
                    qualified_name=f"{repo_alias}/{mkt.name}",
                    repo_alias=repo_alias,
                    marketplace_name=mkt.name,
                    manifest_path=mkt.manifest_path,
                    owner_name=mkt.owner_name,
                    owner_url=mkt.owner_url,
                    title=mkt.title,
                    description=mkt.description,
                    indexed_at_sha=result.sha,
                )
            )
        for plugin in result.plugins:
            session.add(
                PluginIndex(
                    qualified_name=f"{repo_alias}/{plugin.name}",
                    repo_alias=repo_alias,
                    plugin_name=plugin.name,
                    flavor=plugin.kind,  # the field stores the kind name
                    source_path=plugin.source_path,
                    marketplace_name=plugin.marketplace_name,
                    version=plugin.version,
                    description=plugin.description,
                    category=plugin.category,
                    keywords=",".join(plugin.keywords),
                    indexed_at_sha=result.sha,
                )
            )
        session.commit()
    return result


def index_row(
    qualified_name: str, flavor: str | None = None, project_root: Path | None = None
) -> PluginIndex:
    """Return the PluginIndex row for a discoverable plugin.

    A name can resolve to more than one row when multiple kinds expose it; pass
    ``flavor`` to disambiguate. When ``project_root`` is given, plugins discovered by
    the project's ``.aim/targets`` specs are considered too (not just the global index),
    so a project-scoped target can be installed. Raises PluginNotIndexedError if none
    match, or PluginAmbiguousFlavorError if several match and no flavor was given.
    """
    rows = [
        r for r in list_plugins(project_root=project_root) if r.qualified_name == qualified_name
    ]
    if flavor is not None:
        rows = [r for r in rows if r.flavor == flavor]
    if not rows:
        raise PluginNotIndexedError(qualified_name)
    if len(rows) > 1:
        raise PluginAmbiguousFlavorError(qualified_name, sorted(r.flavor for r in rows))
    return rows[0]


def read_plugin_content(
    qualified_name: str, flavor: str | None = None, project_root: Path | None = None
) -> str:
    """Return a human-readable manifest for a discoverable plugin.

    Claude plugins show their ``plugin.json`` when present; declarative kinds show
    their declared manifest file. ``project_root`` includes the project's
    ``.aim/targets`` plugins in the lookup.
    """
    row = index_row(qualified_name, flavor, project_root)
    repo_dir = repos.clone_dir(row.repo_alias)
    backend = git.get_backend()
    if row.flavor == "claude":
        manifest_path = f"{row.source_path}/.claude-plugin/plugin.json"
        try:
            return backend.cat_file(repo_dir, row.indexed_at_sha, manifest_path)
        except git.GitError:
            return f"(no plugin.json found under {row.source_path})"
    kind = plugin_kinds.get_kind(row.flavor, project_root)
    if isinstance(kind, plugin_kinds.DeclarativeKind):
        fname = kind.spec.manifest.file
        manifest_path = f"{row.source_path}/{fname}" if row.source_path else fname
        try:
            return backend.cat_file(repo_dir, row.indexed_at_sha, manifest_path)
        except git.GitError:
            return f"(no {fname} found under {row.source_path or '.'})"
    return f"({row.flavor} plugin at {row.source_path})"


def _project_target_rows(
    project_root: Path,
    indexed: list[PluginIndex],
    repo_alias: str | None,
    marketplace: str | None,
    flavor: str | None,
) -> list[PluginIndex]:
    """Live-discover plugins from project-only targets as in-memory PluginIndex rows.

    Machine-global indexing applies only built-in + global targets, so a target spec in
    the project's ``.aim/targets`` would otherwise never surface in `plugin list`. This
    overlays its plugins at list time without persisting them to the global index.

    Args:
        project_root: The project whose ``.aim/targets`` specs to apply.
        indexed: The already-collected indexed rows (used to skip duplicates).
        repo_alias: Restrict discovery to this repo when given.
        marketplace: Marketplace-name filter to honor (kept consistent with the query).
        flavor: Kind-name filter to honor.
    """
    only = {
        name: kind
        for name, kind in plugin_kinds.load_kinds(project_root).items()
        if name not in plugin_kinds.load_kinds()
    }
    if not only:
        return []
    seen = {(r.qualified_name, r.flavor) for r in indexed}
    aliases = [repo_alias] if repo_alias is not None else [r.alias for r in repos.list_repos()]
    out: list[PluginIndex] = []
    for alias in aliases:
        try:
            res = _discover_in_repo(alias, only)
        except git.GitError:
            continue  # a repo whose clone is missing/broken is skipped, never fatal
        for plugin in res.plugins:
            key = (f"{alias}/{plugin.name}", plugin.kind)
            if key in seen:
                continue
            if flavor is not None and plugin.kind != flavor:
                continue
            if marketplace is not None and plugin.marketplace_name != marketplace:
                continue
            seen.add(key)
            out.append(
                PluginIndex(
                    qualified_name=key[0],
                    repo_alias=alias,
                    plugin_name=plugin.name,
                    flavor=plugin.kind,
                    source_path=plugin.source_path,
                    marketplace_name=plugin.marketplace_name,
                    version=plugin.version,
                    description=plugin.description,
                    category=plugin.category,
                    keywords=",".join(plugin.keywords),
                    indexed_at_sha=res.sha,
                )
            )
    return out


def list_plugins(
    repo_alias: str | None = None,
    marketplace: str | None = None,
    flavor: str | None = None,
    project_root: Path | None = None,
) -> list[PluginIndex]:
    """Return indexed plugins sorted by qualified name, with optional filters.

    Args:
        repo_alias: If given, restrict to this repo's plugins.
        marketplace: If given, restrict to plugins from this marketplace name.
        flavor: If given, restrict to this kind name (e.g. "claude", "opencode").
        project_root: If given, also overlay plugins discovered by the project's own
            ``.aim/targets`` specs (machine-global indexing does not cover them).

    Returns:
        The matching PluginIndex rows, sorted by qualified name.
    """
    with db.session() as session:
        stmt = select(PluginIndex)
        if repo_alias is not None:
            stmt = stmt.where(PluginIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        if marketplace is not None:
            stmt = stmt.where(PluginIndex.marketplace_name == marketplace)  # type: ignore[arg-type]
        if flavor is not None:
            stmt = stmt.where(PluginIndex.flavor == flavor)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    if project_root is not None:
        rows.extend(_project_target_rows(project_root, rows, repo_alias, marketplace, flavor))
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def list_marketplaces(repo_alias: str | None = None) -> list[MarketplaceIndex]:
    """Return indexed marketplaces sorted by qualified name, optionally by repo."""
    with db.session() as session:
        stmt = select(MarketplaceIndex)
        if repo_alias is not None:
            stmt = stmt.where(MarketplaceIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str, project_root: Path | None = None) -> list[PluginIndex]:
    """Case-insensitive substring search across name, description, keywords.

    Args:
        query: Substring to match.
        project_root: If given, also search the project's ``.aim/targets`` plugins.
    """
    q = query.strip().lower()
    if not q:
        return list_plugins(project_root=project_root)
    out: list[PluginIndex] = []
    for row in list_plugins(project_root=project_root):
        haystack = " ".join(
            filter(None, [row.qualified_name, row.description, row.category, row.keywords])
        ).lower()
        if q in haystack:
            out.append(row)
    return out
