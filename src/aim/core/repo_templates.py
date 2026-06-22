"""Shareable project-template discovery from registered source repos.

A template is a `templates/<name>.toml` (or `.aim/templates/<name>.toml`) file
holding a serialized Profile (see `aim.core.profiles`). Discovery is persisted in
the SQLite `TemplateIndex` table and rebuilt from the cached bare clone's default
ref on refresh, mirroring rule/archetype discovery.

If the same template name appears at multiple locations, the shallower path wins
(`templates/` over `.aim/templates/`); the other is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from sqlmodel import delete, select

from aim.core import db, git, profiles, repos, validation
from aim.core.models import TemplateIndex

# Only canonical template locations are discovered.
_TEMPLATE_PREFIXES = ("templates/", ".aim/templates/")
_TEMPLATE_RE = re.compile(r"^(?:(?P<path>.*)/)?(?P<name>[^/]+)\.toml$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TemplateNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the template index."""


def _prefix_rank(path: str) -> int:
    """Return precedence for a template path's location prefix (lower wins)."""
    return 0 if path.startswith("templates/") else 1


class DiscoveredTemplate(NamedTuple):
    """A template found in a repo: its name and source `.toml` path."""

    name: str
    template_toml_path: str
    title: str | None
    description: str | None


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering templates in a repo at a resolved SHA."""

    repo_alias: str
    sha: str
    indexed: list[DiscoveredTemplate]
    shadowed: list[DiscoveredTemplate]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    """Discover installable templates in a registered repo at its default ref.

    Each candidate `.toml` is parsed and validated as a Profile; files that fail
    to parse are skipped (not surfaced as templates).

    Args:
        repo_alias: Alias of the registered source repo to scan.

    Returns:
        An IndexResult with the winning templates and any shadowed duplicates.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    by_name: dict[str, list[tuple[tuple[int, int, str], DiscoveredTemplate]]] = {}
    for p in paths:
        if not p.startswith(_TEMPLATE_PREFIXES):
            continue
        match = _TEMPLATE_RE.match(p)
        if not match:
            continue
        if not validation.is_safe_repo_path(p):
            continue
        name = match.group("name")
        if not _NAME_RE.fullmatch(name):
            continue
        try:
            body = git.get_backend().cat_file(repo_dir, sha, p)
            profile = profiles.parse_toml(body, source=p)
        except (git.GitError, profiles.ProfileTomlError):
            continue
        depth = p.count("/")
        by_name.setdefault(name, []).append(
            (
                (depth, _prefix_rank(p), p),
                DiscoveredTemplate(
                    name=name,
                    template_toml_path=p,
                    title=profile.name,
                    description=profile.description,
                ),
            )
        )

    indexed: list[DiscoveredTemplate] = []
    shadowed: list[DiscoveredTemplate] = []
    for _, candidates in sorted(by_name.items()):
        candidates.sort(key=lambda c: c[0])
        indexed.append(candidates[0][1])
        shadowed.extend(c[1] for c in candidates[1:])

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover templates in a registered repo and write TemplateIndex rows.

    Args:
        repo_alias: Alias of the registered source repo to index.

    Returns:
        The IndexResult produced by discovery.
    """
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(delete(TemplateIndex).where(TemplateIndex.repo_alias == repo_alias))  # type: ignore[arg-type]
        for tmpl in result.indexed:
            session.add(
                TemplateIndex(
                    qualified_name=f"{repo_alias}/{tmpl.name}",
                    repo_alias=repo_alias,
                    template_name=tmpl.name,
                    template_toml_path=tmpl.template_toml_path,
                    title=tmpl.title,
                    description=tmpl.description,
                    indexed_at_sha=result.sha,
                )
            )
        session.commit()
    return result


def index_row(qualified_name: str) -> TemplateIndex:
    """Return the TemplateIndex row for an indexed template, or raise."""
    with db.session() as session:
        row = session.get(TemplateIndex, qualified_name)
    if row is None:
        raise TemplateNotIndexedError(qualified_name)
    return row


def read_template_content(qualified_name: str) -> str:
    """Return the raw template .toml content for an indexed template."""
    row = index_row(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, row.template_toml_path)


def load_template(qualified_name: str) -> profiles.Profile:
    """Load and parse an indexed repo template into a Profile."""
    return profiles.parse_toml(read_template_content(qualified_name), source=qualified_name)


def list_templates(repo_alias: str | None = None) -> list[TemplateIndex]:
    """Return indexed templates sorted by qualified name, optionally by repo.

    Args:
        repo_alias: If given, restrict results to this repo's templates.

    Returns:
        The matching TemplateIndex rows, sorted by qualified name.
    """
    with db.session() as session:
        stmt = select(TemplateIndex)
        if repo_alias is not None:
            stmt = stmt.where(TemplateIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str) -> list[TemplateIndex]:
    """Case-insensitive substring search across qualified_name, title, description."""
    q = query.strip().lower()
    if not q:
        return list_templates()
    out: list[TemplateIndex] = []
    for row in list_templates():
        haystack = " ".join(filter(None, [row.qualified_name, row.title, row.description])).lower()
        if q in haystack:
            out.append(row)
    return out
