"""Plugin-target discovery from registered source repos.

A *target* is a declarative plugin-kind TOML (the spec parsed as
``plugin_kinds.KindSpec``) that teaches aim how to discover and install a
particular client's plugins. A repo shares targets by committing them under a
``targets/`` directory; each ``targets/*.toml`` that validates as a KindSpec is
an installable target named by the spec's ``name`` field (not the filename).

Installing a target (``aim target add``) vendors the TOML into the project's
``.aim/targets/<name>.toml`` and SHA-pins it in ``aim.lock.toml`` — the same
load directory aim already reads kinds from, so the target is active on install
with no extra wiring. A target is config, not agent-facing instructions, so it
is not risk-scanned (the spec's paths are validated at parse time instead).

Discovery results are persisted in the SQLite ``TargetIndex`` table.
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass
from typing import NamedTuple

from sqlmodel import delete, select

from aim.core import db, git, repos, validation
from aim.core.models import TargetIndex

# Only canonical target locations are discovered. Arbitrary `*.toml` elsewhere in
# a repo (configs, fixtures) must not surface as installable targets.
_TARGET_PREFIX = "targets/"

_skipped_warnings: list[str] = []
_skipped_lock = threading.Lock()


def take_index_warnings() -> list[str]:
    """Drain and return warnings about target specs skipped during indexing."""
    with _skipped_lock:
        out = list(_skipped_warnings)
        _skipped_warnings.clear()
    return out


def _warn(message: str) -> None:
    with _skipped_lock:
        _skipped_warnings.append(message)


class DiscoveredTarget(NamedTuple):
    """A target found in a repo: its declared name and source `.toml` path."""

    name: str  # the KindSpec.name declared in the TOML
    target_toml_path: str  # path of the .toml file relative to repo root


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering targets in a repo at a resolved SHA."""

    repo_alias: str
    sha: str
    indexed: list[DiscoveredTarget]
    shadowed: list[DiscoveredTarget]  # skipped duplicates at lower precedence


def _parse_target(repo_alias: str, sha: str, path: str) -> str | None:
    """Return a valid target name for a `targets/*.toml`, or None when unusable.

    Parses the file as a ``KindSpec`` (which enforces the spec-time path
    validation), and records a skip warning on any failure so the user learns why
    a file did not become an installable target.
    """
    from aim.core.plugin_kinds import KindSpec  # lazy import avoids a load cycle

    repo_dir = repos.clone_dir(repo_alias)
    try:
        raw = git.get_backend().cat_file(repo_dir, sha, path)
    except git.GitError as exc:
        _warn(f"{repo_alias}: skipped target {path}: {exc}")
        return None
    try:
        spec = KindSpec.model_validate(tomllib.loads(raw))
    except Exception as exc:
        _warn(f"{repo_alias}: ignored invalid target spec {path}: {exc}")
        return None
    if not validation.is_valid_plugin_name(spec.name):
        _warn(f"{repo_alias}: {path}: invalid target name {spec.name!r}")
        return None
    return spec.name


def discover(repo_alias: str) -> IndexResult:
    """Discover installable targets in a registered repo at its default ref.

    Args:
        repo_alias: Alias of the registered source repo to scan.

    Returns:
        An IndexResult with the winning targets and any shadowed duplicates.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    # Group candidates by declared target name. Precedence: shallower path wins,
    # ties broken lexicographically (mirrors rule discovery).
    by_name: dict[str, list[tuple[tuple[int, str], DiscoveredTarget]]] = {}
    for p in paths:
        if not p.startswith(_TARGET_PREFIX) or not p.endswith(".toml"):
            continue
        if not validation.is_safe_repo_path(p):
            continue
        name = _parse_target(repo_alias, sha, p)
        if name is None:
            continue
        depth = p.count("/")
        by_name.setdefault(name, []).append(
            ((depth, p), DiscoveredTarget(name=name, target_toml_path=p))
        )

    indexed: list[DiscoveredTarget] = []
    shadowed: list[DiscoveredTarget] = []
    for _, candidates in sorted(by_name.items()):
        candidates.sort(key=lambda c: c[0])
        indexed.append(candidates[0][1])
        shadowed.extend(c[1] for c in candidates[1:])

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover targets in a registered repo and write TargetIndex rows.

    Args:
        repo_alias: Alias of the registered source repo to index.

    Returns:
        The IndexResult produced by discovery.
    """
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(delete(TargetIndex).where(TargetIndex.repo_alias == repo_alias))  # type: ignore[arg-type]
        for target in result.indexed:
            session.add(
                TargetIndex(
                    qualified_name=f"{repo_alias}/{target.name}",
                    repo_alias=repo_alias,
                    target_name=target.name,
                    target_toml_path=target.target_toml_path,
                    title=target.name,
                    description=None,
                    indexed_at_sha=result.sha,
                )
            )
        session.commit()
    return result


class TargetNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the target index."""


def index_row(qualified_name: str) -> TargetIndex:
    """Return the TargetIndex row for an indexed target, or raise."""
    with db.session() as session:
        row = session.get(TargetIndex, qualified_name)
    if row is None:
        raise TargetNotIndexedError(qualified_name)
    return row


def read_target_content(qualified_name: str) -> str:
    """Return the raw target .toml content for an indexed target."""
    row = index_row(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, row.target_toml_path)


def list_targets(repo_alias: str | None = None) -> list[TargetIndex]:
    """Return indexed targets sorted by qualified name, optionally filtered by repo."""
    with db.session() as session:
        stmt = select(TargetIndex)
        if repo_alias is not None:
            stmt = stmt.where(TargetIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str) -> list[TargetIndex]:
    """Case-insensitive substring search across qualified_name, title, description."""
    q = query.strip().lower()
    if not q:
        return list_targets()
    out: list[TargetIndex] = []
    for row in list_targets():
        haystack = " ".join(filter(None, [row.qualified_name, row.title, row.description])).lower()
        if q in haystack:
            out.append(row)
    return out
