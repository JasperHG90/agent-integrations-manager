"""Project-instruction archetypes: canonical discovery plus explicit links.

An archetype holds one or more standard instruction files — AGENTS.md,
CLAUDE.md, GEMINI.md, or OPENCODE.md. It is a selectable *base* for a
project's AGENTS.md: choosing one supplies the body that aim's managed regions
(rules, etc.) are merged into.

Archetypes are explicit. Only the canonical authoring locations
`instructions/<name>/` and `.aim/instructions/<name>/` are auto-discovered;
an instruction file sitting anywhere else in a repo (its root, docs/, ...)
does NOT count as an archetype unless it is registered as an explicit link
(`register_link`, e.g. via `aim archetype use <blob-url>`). Link rows survive
reindexing and are recreated from the lockfile on a teammate's `aim sync`, so
an explicitly linked archetype travels with the project.

The base file is chosen by priority (AGENTS.md first); the others are recorded as
`available` for visibility. Discovery is persisted in the SQLite `ArchetypeIndex`
table and rebuilt from the cached bare clone's default ref on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlmodel import delete, or_, select

from aim.core import db, git, repos, validation
from aim.core.models import ArchetypeIndex

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is required but be defensive
    yaml = None  # type: ignore[assignment]

# Standard instruction filenames, in base-selection priority order.
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "OPENCODE.md")
_FILE_ALT = "|".join(re.escape(f) for f in _INSTRUCTION_FILES)
# Only the canonical authoring locations are discovered: `instructions/<name>/`
# and `.aim/instructions/<name>/`. Instruction files anywhere else in a repo do
# not count as archetypes unless explicitly registered via `register_link`.
_ARCHETYPE_RE = re.compile(rf"^(?P<dir>(?:\.aim/)?instructions/[^/]+)/(?P<file>{_FILE_ALT})$")
_CANONICAL_RE = re.compile(r"^instructions/[^/]+$")

# ArchetypeIndex.indexed_via values. NULL (legacy rows) is treated as
# discovered. Deliberately NOT named `origin`: the skill/agent/rule index
# tables use that name for the origins.py vocabulary (canonical/dot-claude/…),
# which is a different axis entirely.
VIA_DISCOVERED = "discovered"
VIA_LINK = "link"


def _dir_rank(source_dir: str) -> int:
    """Return precedence for an archetype directory (lower wins on name collisions)."""
    return 0 if _CANONICAL_RE.fullmatch(source_dir) else 1


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
    return _discover_in_tree(repo_alias, sha, paths)


def _discover_in_tree(repo_alias: str, sha: str, paths: list[str]) -> IndexResult:
    """Discover archetypes in an already-listed tree (no extra git calls).

    Split from `discover` so `index_repo` — which needs the tree itself for
    link-row liveness checks — walks the repo exactly once per reindex.
    """
    # Map each archetype directory to the instruction files it contains. The
    # archetype name is the directory's last path component.
    dirs: dict[str, dict[str, str]] = {}  # source_dir -> {filename: full_path}
    dir_name: dict[str, str] = {}  # source_dir -> archetype name
    for p in paths:
        match = _ARCHETYPE_RE.match(p)
        if not match:
            continue
        if not validation.is_safe_repo_path(p):
            continue
        source_dir = match.group("dir")
        name = source_dir.rsplit("/", 1)[-1]
        if not validation.is_valid_archetype_name(name):
            continue
        dirs.setdefault(source_dir, {})[match.group("file")] = p
        dir_name[source_dir] = name

    # Group directories by archetype name; the canonical/shallowest location wins.
    by_name: dict[str, list[str]] = {}
    for source_dir, name in dir_name.items():
        by_name.setdefault(name, []).append(source_dir)

    indexed: list[DiscoveredArchetype] = []
    shadowed: list[DiscoveredArchetype] = []
    for _, source_dirs in sorted(by_name.items()):
        source_dirs.sort(key=lambda d: (_dir_rank(d), d.count("/"), d))
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

    Discovered rows are rebuilt from scratch. Explicitly linked rows
    (`indexed_via == "link"`) whose file still exists at the new SHA are
    refreshed in place and shadow a discovered archetype of the same name;
    links whose file vanished upstream are PRUNED — a dead link listed forever
    (pinned to a SHA a fresh clone may not even have) would permanently shadow
    a valid canonical archetype of the same name. A project that selected the
    linked archetype keeps rendering from its lockfile pin regardless.

    Args:
        repo_alias: Alias of the registered source repo to index.

    Returns:
        The IndexResult produced by discovery.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    tree_list = git.get_backend().ls_tree(repo_dir, sha)  # the ONE tree walk
    result = _discover_in_tree(repo_alias, sha, tree_list)
    tree = set(tree_list)
    with db.session() as session:
        links = list(
            session.exec(
                select(ArchetypeIndex).where(
                    ArchetypeIndex.repo_alias == repo_alias,  # type: ignore[arg-type]
                    ArchetypeIndex.indexed_via == VIA_LINK,  # type: ignore[arg-type]
                )
            ).all()
        )
        live_links = [row for row in links if row.instruction_path in tree]
        linked_names = {row.qualified_name for row in live_links}
        session.exec(
            delete(ArchetypeIndex).where(
                ArchetypeIndex.repo_alias == repo_alias,  # type: ignore[arg-type]
                or_(
                    ArchetypeIndex.indexed_via != VIA_LINK,  # type: ignore[arg-type]
                    ArchetypeIndex.indexed_via.is_(None),  # type: ignore[union-attr]
                ),
            )
        )
        for row in links:
            if row not in live_links:
                session.delete(row)  # dead link: file gone upstream
                continue
            title, description = _parse_instruction_md(repo_alias, sha, row.instruction_path)
            row.title = title
            row.description = description
            row.available = ",".join(_available_in_dir(tree, row.source_path))
            row.indexed_at_sha = sha
            session.add(row)
        for archetype in result.indexed:
            if f"{repo_alias}/{archetype.name}" in linked_names:
                continue  # a live explicit link owns this name
            title, description = _parse_instruction_md(repo_alias, sha, archetype.instruction_path)
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
                    indexed_at_sha=sha,
                    indexed_via=VIA_DISCOVERED,
                )
            )
        session.commit()
    return result


def _available_in_dir(tree: set[str], source_dir: str) -> list[str]:
    """Return the standard instruction filenames present in a directory.

    Args:
        tree: All file paths in the repo tree at the relevant SHA.
        source_dir: Directory relative to the repo root; "" means the root.

    Returns:
        The standard filenames present, in base-selection priority order.
    """
    prefix = f"{source_dir}/" if source_dir else ""
    return [f for f in _INSTRUCTION_FILES if f"{prefix}{f}" in tree]


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


class ArchetypeLinkError(ValueError):
    """Raised when an explicit archetype link cannot be registered."""


def is_instruction_filename(name: str) -> bool:
    """Return whether `name` is one of the standard instruction filenames."""
    return name in _INSTRUCTION_FILES


def canonical_archetype_name(path: str) -> str | None:
    """Return the archetype name when `path` is a canonical instructions/ location.

    Matches `instructions/<name>`, `.aim/instructions/<name>`, and a standard
    instruction file directly inside one. Canonical locations are auto-discovered,
    so callers route them through the discovered flow instead of writing a
    redundant (and stickier) link row. A flat `instructions/AGENTS.md` (no
    per-archetype directory) is NOT canonical — discovery would find nothing
    there, so it stays linkable.
    """
    match = re.fullmatch(
        rf"(?:\.aim/)?instructions/(?P<name>[^/]+)(?:/(?:{_FILE_ALT}))?",
        path.strip().strip("/"),
    )
    if match is None or is_instruction_filename(match.group("name")):
        return None
    return match.group("name")


def register_link(repo_alias: str, path: str, *, name: str | None = None) -> ArchetypeIndex:
    """Explicitly register an instruction file (or a directory holding one) as an archetype.

    This is how an instruction file OUTSIDE the canonical `instructions/`
    locations — including a repo-root AGENTS.md — becomes selectable: discovery
    deliberately ignores such files unless they are linked explicitly.

    Args:
        repo_alias: Alias of the registered source repo containing the file.
        path: Repo-relative path to a standard instruction file (AGENTS.md /
            CLAUDE.md / GEMINI.md / OPENCODE.md) or to a directory containing
            at least one of them.
        name: Archetype name to register under. Defaults to the file's parent
            directory name (the directory name for directory links), or the
            repo alias for a root-level file.

    Returns:
        The upserted ArchetypeIndex row (`indexed_via == "link"`).

    Raises:
        ArchetypeLinkError: If the path is unsafe, does not exist at the repo's
            tracked SHA, is not a standard instruction file or a directory
            containing one, is a canonical (auto-discovered) location, names a
            discovered archetype, or the derived name is invalid.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    tree = set(git.get_backend().ls_tree(repo_dir, sha))

    norm = path.strip().strip("/")
    if not norm or not validation.is_safe_repo_path(norm):
        raise ArchetypeLinkError(f"{repo_alias}: invalid instruction path {path!r}")
    # The "canonical stays discovered" invariant is enforced HERE, not just in
    # the CLI routing: any caller (sync's index-recreation included) linking a
    # canonical path would create a sticky row that shadows the self-healing
    # discovered one.
    canonical = canonical_archetype_name(norm)
    if canonical is not None:
        raise ArchetypeLinkError(
            f"{repo_alias}: {norm!r} is a canonical instructions/ location — it is "
            f"auto-discovered; select {repo_alias}/{canonical} directly"
        )

    if norm in tree:
        base = norm.rsplit("/", 1)[-1]
        if base not in _INSTRUCTION_FILES:
            raise ArchetypeLinkError(
                f"{repo_alias}: {norm!r} is not a standard instruction file "
                f"({', '.join(_INSTRUCTION_FILES)})"
            )
        source_dir = norm.rsplit("/", 1)[0] if "/" in norm else ""
        instruction_path = norm
    else:
        available_here = _available_in_dir(tree, norm)
        if not available_here:
            raise ArchetypeLinkError(
                f"{repo_alias}: {norm!r} does not exist at {sha[:12]} (or holds no "
                f"standard instruction file)"
            )
        source_dir = norm
        instruction_path = f"{norm}/{available_here[0]}"

    resolved_name = name or (source_dir.rsplit("/", 1)[-1] if source_dir else repo_alias)
    if not validation.is_valid_archetype_name(resolved_name):
        raise ArchetypeLinkError(
            f"{repo_alias}: cannot derive a valid archetype name from {path!r}; "
            f"pass an explicit name (lowercase alphanumeric, _, or -)"
        )

    title, description = _parse_instruction_md(repo_alias, sha, instruction_path)
    qualified_name = f"{repo_alias}/{resolved_name}"
    with db.session() as session:
        row = session.get(ArchetypeIndex, qualified_name)
        if row is not None and row.indexed_via != VIA_LINK:
            # NULL (legacy) rows count as discovered too. Rebinding a discovered
            # name would defeat allow-lists keyed on qualified_name and survive
            # every refresh — the name is taken; pick another.
            raise ArchetypeLinkError(
                f"{qualified_name} is a discovered archetype; pass a different name "
                f"for the link (e.g. --path with NAME)"
            )
        if row is None:
            row = ArchetypeIndex(
                qualified_name=qualified_name,
                repo_alias=repo_alias,
                archetype_name=resolved_name,
                source_path=source_dir,
                instruction_path=instruction_path,
                available=",".join(_available_in_dir(tree, source_dir)),
                title=title,
                description=description,
                indexed_at_sha=sha,
                indexed_via=VIA_LINK,
            )
        else:
            row.source_path = source_dir
            row.instruction_path = instruction_path
            row.available = ",".join(_available_in_dir(tree, source_dir))
            row.title = title
            row.description = description
            row.indexed_at_sha = sha
            row.indexed_via = VIA_LINK
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


class ArchetypeNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the archetype index."""

    def __str__(self) -> str:  # KeyError's default is the quoted key — useless
        return (
            f"archetype {self.args[0]!r} is not indexed — check `aim archetype list`, "
            f"or run `aim repo refresh <alias>` / `aim sync` to rebuild the index"
        )


def unregister_link(qualified_name: str) -> None:
    """Remove an explicitly registered archetype link from the index.

    Only link rows can be removed this way; discovered rows are owned by
    discovery and would reappear on the next index anyway. Projects that
    selected the archetype are untouched — their pin lives in aim.toml /
    aim.lock.toml, not in this index (and a project whose lockfile still
    selects the link re-registers it on its next `aim sync`).

    Raises:
        ArchetypeNotIndexedError: If the qualified name is not indexed.
        ArchetypeLinkError: If the row is a discovered archetype, not a link.
    """
    with db.session() as session:
        row = session.get(ArchetypeIndex, qualified_name)
        if row is None:
            raise ArchetypeNotIndexedError(qualified_name)
        if row.indexed_via != VIA_LINK:
            raise ArchetypeLinkError(
                f"{qualified_name} is a discovered archetype, not an explicit link"
            )
        session.delete(row)
        session.commit()


def index_row(qualified_name: str) -> ArchetypeIndex:
    """Return the ArchetypeIndex row for an indexed archetype, or raise."""
    with db.session() as session:
        row = session.get(ArchetypeIndex, qualified_name)
    if row is None:
        raise ArchetypeNotIndexedError(qualified_name)
    return row


def read_base_body(repo_alias: str, sha: str, instruction_path: str) -> str:
    """Return the archetype's base instruction body at a pinned SHA.

    Strips any YAML frontmatter (used only for the archetype's title/description in
    the index) so it never leaks into the rendered project AGENTS.md.
    """
    repo_dir = repos.clone_dir(repo_alias)
    raw = git.get_backend().cat_file(repo_dir, sha, instruction_path)
    _frontmatter, body = _extract_frontmatter(raw)
    return body


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
