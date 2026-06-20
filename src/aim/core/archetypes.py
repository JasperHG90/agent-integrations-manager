"""Project-instruction archetype discovery from registered source repos.

An archetype is a directory `instructions/<name>/` (or `.aim/instructions/<name>/`,
deliberately never the repo root) holding one or more standard instruction files —
AGENTS.md, CLAUDE.md, GEMINI.md, or OPENCODE.md. It is a selectable *base* for a
project's AGENTS.md: choosing one supplies the body that aim's managed regions
(rules, etc.) are merged into.

The base file is chosen by priority (AGENTS.md first); the others are recorded as
`available` for visibility. Discovery is persisted in the SQLite `ArchetypeIndex`
table and rebuilt from the cached bare clone's default ref on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlmodel import delete, select

from aim.core import db, git, repos, validation
from aim.core.models import ArchetypeIndex

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is required but be defensive
    yaml = None  # type: ignore[assignment]

# Standard instruction filenames, in base-selection priority order.
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "OPENCODE.md")
_FILE_ALT = "|".join(re.escape(f) for f in _INSTRUCTION_FILES)
# Archetypes live under a fixed subdirectory, never the repo root.
_ARCHETYPE_RE = re.compile(
    rf"^(?P<prefix>instructions|\.aim/instructions)/(?P<name>[^/]+)/(?P<file>{_FILE_ALT})$"
)


def _prefix_rank(prefix: str) -> int:
    """Return precedence for an archetype location prefix (lower wins)."""
    return 0 if prefix == "instructions" else 1


class DiscoveredArchetype(NamedTuple):
    """An archetype found in a repo: its name, directory, base file, and contents."""

    name: str
    source_path: str  # the archetype directory relative to repo root
    instruction_path: str  # the chosen base instruction file relative to repo root
    available: list[str]  # standard filenames present, in priority order


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering archetypes in a repo at a resolved SHA."""

    repo_alias: str
    sha: str
    indexed: list[DiscoveredArchetype]
    shadowed: list[DiscoveredArchetype]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    """Discover instruction archetypes in a registered repo at its default ref.

    Args:
        repo_alias: Alias of the registered source repo to scan.

    Returns:
        An IndexResult with the winning archetypes and any shadowed duplicates.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    # Map each archetype directory to the instruction files it contains.
    dirs: dict[str, dict[str, str]] = {}  # source_dir -> {filename: full_path}
    dir_prefix: dict[str, str] = {}  # source_dir -> location prefix
    dir_name: dict[str, str] = {}  # source_dir -> archetype name
    for p in paths:
        match = _ARCHETYPE_RE.match(p)
        if not match:
            continue
        if not validation.is_safe_repo_path(p):
            continue
        name = match.group("name")
        if not validation.is_valid_archetype_name(name):
            continue
        source_dir = f"{match.group('prefix')}/{name}"
        dirs.setdefault(source_dir, {})[match.group("file")] = p
        dir_prefix[source_dir] = match.group("prefix")
        dir_name[source_dir] = name

    # Group directories by archetype name; the shallowest/canonical location wins.
    by_name: dict[str, list[str]] = {}
    for source_dir, name in dir_name.items():
        by_name.setdefault(name, []).append(source_dir)

    indexed: list[DiscoveredArchetype] = []
    shadowed: list[DiscoveredArchetype] = []
    for _, source_dirs in sorted(by_name.items()):
        source_dirs.sort(key=lambda d: (_prefix_rank(dir_prefix[d]), d))
        for rank, source_dir in enumerate(source_dirs):
            available = [f for f in _INSTRUCTION_FILES if f in dirs[source_dir]]
            archetype = DiscoveredArchetype(
                name=dir_name[source_dir],
                source_path=source_dir,
                instruction_path=dirs[source_dir][available[0]],
                available=available,
            )
            (indexed if rank == 0 else shadowed).append(archetype)

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover archetypes in a registered repo and write ArchetypeIndex rows.

    Args:
        repo_alias: Alias of the registered source repo to index.

    Returns:
        The IndexResult produced by discovery.
    """
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(
            delete(ArchetypeIndex).where(ArchetypeIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        )
        for archetype in result.indexed:
            title, description = _parse_instruction_md(
                repo_alias, result.sha, archetype.instruction_path
            )
            session.add(
                ArchetypeIndex(
                    qualified_name=f"{repo_alias}/{archetype.name}",
                    repo_alias=repo_alias,
                    archetype_name=archetype.name,
                    source_path=archetype.source_path,
                    instruction_path=archetype.instruction_path,
                    available=",".join(archetype.available),
                    title=title,
                    description=description,
                    indexed_at_sha=result.sha,
                )
            )
        session.commit()
    return result


def _extract_frontmatter(body: str) -> tuple[dict[str, Any], str]:
    """Parse optional YAML frontmatter, returning (fields, remaining body)."""
    fm_match = re.match(r"\A---\n(.*?)\n---\n", body, re.DOTALL)
    if not fm_match:
        return {}, body
    raw = fm_match.group(1)
    body = body[fm_match.end() :]
    if yaml is None:
        return {}, body
    try:
        parsed = yaml.safe_load(raw)
    except Exception:
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


def _as_str(value: Any) -> str | None:
    """Coerce a frontmatter value to a string, preserving None."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _parse_instruction_md(repo_alias: str, sha: str, path: str) -> tuple[str | None, str | None]:
    """Pull a title and description from an archetype's base instruction file.

    Args:
        repo_alias: Alias of the repo containing the archetype.
        sha: Resolved commit SHA to read the file at.
        path: Path of the base instruction file relative to the repo root.

    Returns:
        A tuple of the title and description, each possibly None.
    """
    repo_dir = repos.clone_dir(repo_alias)
    try:
        body = git.get_backend().cat_file(repo_dir, sha, path)
    except git.GitError:
        return None, None
    frontmatter, remainder = _extract_frontmatter(body)
    title = _as_str(frontmatter.get("title") or frontmatter.get("name"))
    description = _as_str(frontmatter.get("description"))
    if title is None:
        for line in remainder.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
    return title, description


class ArchetypeNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the archetype index."""


def index_row(qualified_name: str) -> ArchetypeIndex:
    """Return the ArchetypeIndex row for an indexed archetype, or raise."""
    with db.session() as session:
        row = session.get(ArchetypeIndex, qualified_name)
    if row is None:
        raise ArchetypeNotIndexedError(qualified_name)
    return row


def read_base_body(repo_alias: str, sha: str, instruction_path: str) -> str:
    """Return the archetype's base instruction file content at a pinned SHA."""
    repo_dir = repos.clone_dir(repo_alias)
    return git.get_backend().cat_file(repo_dir, sha, instruction_path)


def list_archetypes(repo_alias: str | None = None) -> list[ArchetypeIndex]:
    """Return indexed archetypes sorted by qualified name, optionally by repo.

    Args:
        repo_alias: If given, restrict results to this repo's archetypes.

    Returns:
        The matching ArchetypeIndex rows, sorted by qualified name.
    """
    with db.session() as session:
        stmt = select(ArchetypeIndex)
        if repo_alias is not None:
            stmt = stmt.where(ArchetypeIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str) -> list[ArchetypeIndex]:
    """Case-insensitive substring search across qualified_name, title, description."""
    q = query.strip().lower()
    if not q:
        return list_archetypes()
    out: list[ArchetypeIndex] = []
    for row in list_archetypes():
        haystack = " ".join(filter(None, [row.qualified_name, row.title, row.description])).lower()
        if q in haystack:
            out.append(row)
    return out
