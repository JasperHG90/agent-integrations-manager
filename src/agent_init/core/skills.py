"""Skill discovery + search.

A skill lives inside a registered repo at one of three fixed paths
(precedence: highest first):

    1. skills/<name>/SKILL.md
    2. .claude/skills/<name>/SKILL.md
    3. <name>/SKILL.md   (at repo root)

If the same skill name appears at multiple locations, the higher-precedence
one wins and the others are ignored (a warning is logged via the returned
result list).

Discovery results are persisted in the SQLite `SkillIndex` table. The index
is a cache only — it is rebuilt on demand from the cached bare clone's HEAD
(or, more precisely, the registered repo's `default_ref`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from sqlmodel import delete, select

from agent_init.core import db, git, repos
from agent_init.core.agents import _as_str, _as_str_list, _extract_frontmatter
from agent_init.core.models import SkillIndex


def split_csv(value: str) -> list[str]:
    """Helper to read a CSV field back into a list."""
    return [p for p in (s.strip() for s in value.split(",")) if p]

PRECEDENCE = (
    "skills",
    ".claude/skills",
    "",  # root-level <name>/SKILL.md
)

# Matches:
#   skills/<name>/SKILL.md          (rank 0)
#   .claude/skills/<name>/SKILL.md  (rank 1)
#   <name>/SKILL.md                 (rank 2)
#   SKILL.md at repo root           (rank 3)
_SKILL_RE = re.compile(
    r"^(?:(?P<prefix>skills/|\.claude/skills/)(?P<name1>[^/]+)/SKILL\.md|"
    r"(?P<name2>[^/]+)/SKILL\.md|"
    r"^SKILL\.md)$"
)


class DiscoveredSkill(NamedTuple):
    name: str
    source_path: str  # path of the skill DIRECTORY relative to repo root
    skill_md_path: str  # path of the SKILL.md file relative to repo root


@dataclass(frozen=True)
class IndexResult:
    repo_alias: str
    sha: str
    indexed: list[DiscoveredSkill]
    shadowed: list[DiscoveredSkill]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    # Group candidates by skill name with precedence-rank ordering.
    by_name: dict[str, list[tuple[int, DiscoveredSkill]]] = {}
    for p in paths:
        match = _SKILL_RE.match(p)
        if not match:
            continue
        prefix = match.group("prefix") or ""
        name = match.group("name1") or match.group("name2")
        if prefix == "skills/":
            rank = 0
        elif prefix == ".claude/skills/":
            rank = 1
        elif name:
            rank = 2
        else:
            rank = 3
        source_dir = p[: -len("/SKILL.md")] if p != "SKILL.md" else ""
        if not name:
            name = repo_alias
        by_name.setdefault(name, []).append(
            (rank, DiscoveredSkill(name=name, source_path=source_dir, skill_md_path=p))
        )

    indexed: list[DiscoveredSkill] = []
    shadowed: list[DiscoveredSkill] = []
    for _, candidates in sorted(by_name.items()):
        candidates.sort(key=lambda c: c[0])
        winner = candidates[0][1]
        indexed.append(winner)
        shadowed.extend(c[1] for c in candidates[1:])

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover skills in a registered repo and write SkillIndex rows.

    Old rows for this repo are deleted before insertion (so renames/removals
    in the upstream repo are reflected on re-index).
    """
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(delete(SkillIndex).where(SkillIndex.repo_alias == repo_alias))
        for skill in result.indexed:
            title, description, prereqs, provides = _parse_skill_md(
                repo_alias, result.sha, skill.skill_md_path
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
                )
            )
        session.commit()
    return result


def _parse_skill_md(
    repo_alias: str, sha: str, path: str
) -> tuple[str | None, str | None, list[str], list[str]]:
    """Pull (title, description, prereqs, provides) from a SKILL.md.

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
    repo_dir = repos.clone_dir(repo_alias)
    try:
        body = git.get_backend().cat_file(repo_dir, sha, path)
    except git.GitError:
        return None, None, [], []

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
    """The requested qualified_name doesn't appear in the skill index."""


def read_skill_content(qualified_name: str) -> str:
    """Return the raw SKILL.md bytes for an indexed skill."""
    with db.session() as session:
        row = session.get(SkillIndex, qualified_name)
    if row is None:
        raise SkillNotIndexedError(qualified_name)
    skill_md_path = row.skill_md_path
    # Legacy indexes written before the skill_md_path column may have NULL
    # here even though source_path is present. Reconstruct the expected path.
    if not skill_md_path and row.source_path:
        skill_md_path = f"{row.source_path}/SKILL.md"
    if not skill_md_path:
        raise SkillNotIndexedError(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, skill_md_path)


def list_skills(repo_alias: str | None = None) -> list[SkillIndex]:
    with db.session() as session:
        stmt = select(SkillIndex)
        if repo_alias is not None:
            stmt = stmt.where(SkillIndex.repo_alias == repo_alias)
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str) -> list[SkillIndex]:
    """Case-insensitive substring search across qualified_name, title, description."""
    q = query.strip().lower()
    if not q:
        return list_skills()
    out: list[SkillIndex] = []
    for row in list_skills():
        haystack = " ".join(
            filter(None, [row.qualified_name, row.title, row.description])
        ).lower()
        if q in haystack:
            out.append(row)
    return out
