"""Plugin and marketplace discovery from registered source repos.

Discovery is driven by the **plugin kind registry** (`plugin_kinds`): each kind
knows what to look for. Built-in kinds (claude) plus any external declarative
kinds (TOML in the global kinds dir) are all consulted. Results are persisted in
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


def discover(repo_alias: str) -> IndexResult:
    """Discover plugins and marketplaces in a registered repo at its default ref.

    Every registered kind (built-in + external) inspects the repo tree once.

    Args:
        repo_alias: Alias of the registered source repo to scan.

    Returns:
        An IndexResult with marketplaces, the winning plugins, and shadowed dupes.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    backend = git.get_backend()
    sha = backend.resolve_ref(repo_dir, repo.default_ref)
    tree = backend.ls_tree(repo_dir, sha)

    kinds = plugin_kinds.load_kinds()  # global kinds only — indexing is machine-global
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


def index_row(qualified_name: str, flavor: str | None = None) -> PluginIndex:
    """Return the PluginIndex row for an indexed plugin.

    A name can resolve to more than one row when multiple kinds expose it; pass
    ``flavor`` to disambiguate. Raises PluginNotIndexedError if none match, or
    PluginAmbiguousFlavorError if several match and no flavor was given.
    """
    with db.session() as session:
        stmt = select(PluginIndex).where(PluginIndex.qualified_name == qualified_name)  # type: ignore[arg-type]
        if flavor is not None:
            stmt = stmt.where(PluginIndex.flavor == flavor)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    if not rows:
        raise PluginNotIndexedError(qualified_name)
    if len(rows) > 1:
        raise PluginAmbiguousFlavorError(qualified_name, sorted(r.flavor for r in rows))
    return rows[0]


def read_plugin_content(qualified_name: str, flavor: str | None = None) -> str:
    """Return a human-readable manifest for an indexed plugin.

    Claude plugins show their ``plugin.json`` when present; other (file-based)
    kinds show the plugin file itself.
    """
    row = index_row(qualified_name, flavor)
    repo_dir = repos.clone_dir(row.repo_alias)
    backend = git.get_backend()
    if row.flavor == "claude":
        manifest_path = f"{row.source_path}/.claude-plugin/plugin.json"
        try:
            return backend.cat_file(repo_dir, row.indexed_at_sha, manifest_path)
        except git.GitError:
            return f"(no plugin.json found under {row.source_path})"
    try:
        return backend.cat_file(repo_dir, row.indexed_at_sha, row.source_path)
    except git.GitError:
        return f"({row.flavor} plugin at {row.source_path})"


def list_plugins(
    repo_alias: str | None = None,
    marketplace: str | None = None,
    flavor: str | None = None,
) -> list[PluginIndex]:
    """Return indexed plugins sorted by qualified name, with optional filters.

    Args:
        repo_alias: If given, restrict to this repo's plugins.
        marketplace: If given, restrict to plugins from this marketplace name.
        flavor: If given, restrict to this kind name (e.g. "claude", "opencode").

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


def search(query: str) -> list[PluginIndex]:
    """Case-insensitive substring search across name, description, keywords."""
    q = query.strip().lower()
    if not q:
        return list_plugins()
    out: list[PluginIndex] = []
    for row in list_plugins():
        haystack = " ".join(
            filter(None, [row.qualified_name, row.description, row.category, row.keywords])
        ).lower()
        if q in haystack:
            out.append(row)
    return out
