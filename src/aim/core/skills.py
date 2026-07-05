"""Skill discovery + search.

A skill is any `SKILL.md` file inside a registered repo. The skill `name` is
the directory containing `SKILL.md`. A bare `SKILL.md` at the repo root uses
the repo alias as its name.

If the same skill name appears at multiple locations, the shallower path
wins (ties broken by lexicographic path); the others are ignored (recorded
as shadowed on the returned result).

Discovery results are persisted in the SQLite `SkillIndex` table. The index
is a cache only — it is rebuilt on demand from the cached bare clone's HEAD
(or, more precisely, the registered repo's `default_ref`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from sqlmodel import delete, select

from aim.core import db, git, origins, repos, validation
from aim.core.agents import _as_str, _as_str_list, _extract_frontmatter
from aim.core.models import SkillIndex


def split_csv(value: str) -> list[str]:
    """Split a comma-separated string into a list of non-empty trimmed parts.

    Args:
        value: Comma-separated field value.

    Returns:
        List of trimmed, non-empty entries.
    """
    return [p for p in (s.strip() for s in value.split(",")) if p]


_SKILL_RE = re.compile(r"^(?P<path>.*)/SKILL\.md$|^SKILL\.md$")


# Canonical paths win at the same depth; arbitrary paths are still discovered.
def _prefix_rank(path: str) -> int:
    """Rank a SKILL.md path by prefix canonicality (lower wins).

    Args:
        path: SKILL.md path relative to the repo root.

    Returns:
        0 for canonical `skills/` or root paths, 1 for `.claude/skills/`,
        2 for any other location.
    """
    if path.startswith("skills/") or path == "SKILL.md":
        return 0
    if path.startswith(".claude/skills/"):
        return 1
    return 2


class DiscoveredSkill(NamedTuple):
    """A skill located during discovery, before it is written to the index."""

    name: str
    source_path: str  # path of the skill DIRECTORY relative to repo root
    skill_md_path: str  # path of the SKILL.md file relative to repo root
    origin: str = origins.ORIGIN_CANONICAL  # provenance (origins.py constants)
    owning_plugin: str | None = None  # bare plugin name when origin == "plugin"


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering skills in a repo: winners and shadowed duplicates."""

    repo_alias: str
    sha: str
    indexed: list[DiscoveredSkill]
    shadowed: list[DiscoveredSkill]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    """Find all SKILL.md skills in a registered repo, resolving duplicates.

    Args:
        repo_alias: Alias of the registered repo to scan.

    Returns:
        An IndexResult holding the resolved SHA, the winning skills, and any
        shadowed duplicates that lost on precedence.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    from aim.core import plugins  # lazy import avoids a module-load cycle

    owned = plugins.owned_dirs(repo_alias, repo_dir, sha, paths)

    # Group candidates by skill name. Precedence: standalone beats plugin-owned;
    # then shallower path wins; at the same depth, canonical prefixes (`skills/`,
    # `.claude/skills/`) win over arbitrary paths. Ties break by lexicographic
    # path. Plugin-owned copies are indexed (origin="plugin", hidden from lists
    # by default) but never outrank a standalone skill of the same name; when
    # both exist the plugin-owned copy lands in `shadowed` (the PK is the
    # qualified name).
    by_name: dict[str, list[tuple[tuple[int, int, int, str], DiscoveredSkill]]] = {}
    for p in paths:
        match = _SKILL_RE.match(p)
        if not match:
            continue

        if not validation.is_safe_repo_path(p):
            continue
        entry = plugins.owning_plugin(p, owned)
        owner = entry[1] if entry is not None else None
        origin = (
            origins.ORIGIN_PLUGIN
            if owner is not None
            else origins.origin_from_rank(_prefix_rank(p))
        )

        path = match.group("path") or ""
        if path:
            if not validation.is_safe_repo_path(path):
                continue
            name = Path(path).name
            source_dir = path
        else:
            name = repo_alias
            source_dir = ""

        if not validation.is_valid_alias(name):
            continue
        depth = p.count("/")
        by_name.setdefault(name, []).append(
            (
                (1 if owner is not None else 0, depth, _prefix_rank(p), p),
                DiscoveredSkill(
                    name=name,
                    source_path=source_dir,
                    skill_md_path=p,
                    origin=origin,
                    owning_plugin=owner,
                ),
            )
        )

    indexed: list[DiscoveredSkill] = []
    shadowed: list[DiscoveredSkill] = []
    for _, candidates in sorted(by_name.items()):
        candidates.sort(key=lambda c: c[0])
        winner = candidates[0][1]
        indexed.append(winner)
        shadowed.extend(c[1] for c in candidates[1:])

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def _index_current(repo_alias: str, sha: str) -> bool:
    """Whether the repo's skill index rows are already current for `sha`.

    Samples one row: current means it was written at `sha` AND by origin-aware
    code (non-NULL origin). Legacy rows force a rebuild so provenance backfills.
    One sample suffices because a repo's rows are written in one transaction.
    """
    with db.session() as session:
        row = session.exec(
            select(SkillIndex.indexed_at_sha, SkillIndex.origin)  # type: ignore[arg-type]
            .where(SkillIndex.repo_alias == repo_alias)
            .limit(1)
        ).first()
    return row is not None and row[0] == sha and row[1] is not None


def index_repo(repo_alias: str) -> IndexResult:
    """Discover skills in a registered repo and write SkillIndex rows.

    Skips the rebuild when the repo is already indexed at the current SHA.
    Otherwise old rows for this repo are deleted before insertion (so
    renames/removals in the upstream repo are reflected on re-index), and every
    SKILL.md is read in one batched git process.
    """
    result = discover(repo_alias)
    if _index_current(repo_alias, result.sha):
        return result
    repo_dir = repos.clone_dir(repo_alias)
    bodies = git.cat_files_text(
        repo_dir, result.sha, [skill.skill_md_path for skill in result.indexed]
    )
    with db.session() as session:
        session.exec(
            delete(SkillIndex).where(SkillIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        )
        for skill in result.indexed:
            body = bodies.get(skill.skill_md_path)
            title, description, prereqs, provides = (
                _parse_skill_md(body) if body is not None else (None, None, [], [])
            )
            session.add(
                SkillIndex(
                    qualified_name=f"{repo_alias}/{skill.name}",
                    repo_alias=repo_alias,
                    skill_name=skill.name,
                    source_path=skill.source_path,
                    skill_md_path=skill.skill_md_path,
                    title=title,
                    description=description,
                    indexed_at_sha=result.sha,
                    prereqs=",".join(prereqs),
                    provides=",".join(provides),
                    origin=skill.origin,
                    owning_plugin=skill.owning_plugin,
                )
            )
        session.commit()
    return result


def _parse_skill_md(
    body: str,
) -> tuple[str | None, str | None, list[str], list[str]]:
    """Pull (title, description, prereqs, provides) from a SKILL.md body.

    Front-matter (optional, must be at top):

        ---
        name: <display name>
        description: <short description>
        prereqs: [other/skill, other/skill2]
        provides: [code-review]
        ---

    `name` and `description` are used when present; otherwise title/description
    heuristics fall back to the body.
    """
    frontmatter, remainder = _extract_frontmatter(body)
    title = _as_str(frontmatter.get("name"))
    description = _as_str(frontmatter.get("description"))
    prereqs = _as_str_list(frontmatter.get("prereqs"))
    provides = _as_str_list(frontmatter.get("provides"))

    lines = remainder.splitlines()
    body_title: str | None = None
    title_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            body_title = stripped[2:].strip()
            title_idx = i
            break
    if body_title is None:
        for line in lines:
            if line.strip():
                body_title = line.strip()
                break
    if title is None:
        title = body_title

    if description is None and title_idx is not None:
        para: list[str] = []
        for line in lines[title_idx + 1 :]:
            stripped = line.strip()
            if not stripped:
                if para:
                    break
                continue
            if stripped.startswith("#") or stripped == "---" or stripped.startswith("|"):
                continue
            para.append(stripped)
        if para:
            description = " ".join(para)

    return title, description, prereqs, provides


class SkillNotIndexedError(KeyError):
    """Raised when the requested qualified_name is absent from the skill index."""


def index_row(qualified_name: str) -> SkillIndex:
    """Return the SkillIndex row for an indexed skill, or raise.

    Raises:
        SkillNotIndexedError: If no index row exists for the qualified name.
    """
    with db.session() as session:
        row = session.get(SkillIndex, qualified_name)
    if row is None:
        raise SkillNotIndexedError(qualified_name)
    return row


def read_skill_content(qualified_name: str) -> str:
    """Return the raw SKILL.md content for an indexed skill.

    Args:
        qualified_name: The `repo_alias/skill_name` key of the indexed skill.

    Returns:
        The SKILL.md file content at the indexed SHA.

    Raises:
        SkillNotIndexedError: If no index row exists or its path is unresolvable.
    """
    with db.session() as session:
        row = session.get(SkillIndex, qualified_name)
    if row is None:
        raise SkillNotIndexedError(qualified_name)
    skill_md_path = row.skill_md_path
    # Legacy indexes written before the skill_md_path column may have NULL
    # here even though source_path is present. Reconstruct the expected path.
    if not skill_md_path and row.source_path:
        skill_md_path = f"{row.source_path}/SKILL.md" if row.source_path else "SKILL.md"
    if not skill_md_path:
        raise SkillNotIndexedError(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, skill_md_path)


def _effective_origin(row: SkillIndex) -> str:
    """A row's origin, derived from the SKILL.md path for legacy (NULL) rows."""
    return origins.derive_origin(row.origin, row.skill_md_path or "", _prefix_rank)


def list_skills(
    repo_alias: str | None = None,
    *,
    include_plugin_owned: bool = True,
    include_dot_claude: bool = True,
) -> list[SkillIndex]:
    """Return indexed skills sorted by qualified name, optionally repo-filtered.

    Args:
        repo_alias: If given, restrict results to this repo's skills.
        include_plugin_owned: Also return skills bundled inside plugins
            (shown by default).
        include_dot_claude: Return skills discovered under `.claude/` dirs
            (shown by default).

    Returns:
        Skill index rows ordered by qualified_name.
    """
    with db.session() as session:
        stmt = select(SkillIndex)
        if repo_alias is not None:
            stmt = stmt.where(SkillIndex.repo_alias == repo_alias)
        rows = list(session.exec(stmt).all())
    rows = [
        r
        for r in rows
        if origins.is_visible(
            _effective_origin(r),
            include_plugin_owned=include_plugin_owned,
            include_dot_claude=include_dot_claude,
        )
    ]
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(
    query: str,
    *,
    include_plugin_owned: bool = True,
    include_dot_claude: bool = True,
) -> list[SkillIndex]:
    """Search indexed skills by case-insensitive substring across key fields.

    Matches against qualified_name, title, and description. An empty query
    returns all skills.

    Args:
        query: Substring to look for.
        include_plugin_owned: Also match skills bundled inside plugins.
        include_dot_claude: Match skills discovered under `.claude/` dirs.

    Returns:
        Matching skill index rows.
    """
    rows = list_skills(
        include_plugin_owned=include_plugin_owned, include_dot_claude=include_dot_claude
    )
    q = query.strip().lower()
    if not q:
        return rows
    out: list[SkillIndex] = []
    for row in rows:
        haystack = " ".join(filter(None, [row.qualified_name, row.title, row.description])).lower()
        if q in haystack:
            out.append(row)
    return out
