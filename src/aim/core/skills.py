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

from aim.core import db, git, repos
from aim.core.agents import _as_str, _as_str_list, _extract_frontmatter
from aim.core.models import SkillIndex
from aim.core.validation import is_valid_alias


def split_csv(value: str) -> list[str]:
    """Helper to read a CSV field back into a list."""
    return [p for p in (s.strip() for s in value.split(",")) if p]


_SKILL_RE = re.compile(r"^(?P<path>.*)/SKILL\.md$|^SKILL\.md$")


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

    # Group candidates by skill name with precedence-rank ordering. Ties on
    # prefix are broken by shallower path, then by lexicographic path.
    by_name: dict[str, list[tuple[tuple[int, str], DiscoveredSkill]]] = {}
    for p in paths:
        match = _SKILL_RE.match(p)
        if not match:
            continue

        path = match.group("path") or ""
        if path:
            name = Path(path).name
            source_dir = path
        else:
            name = repo_alias
            source_dir = ""

        if not is_valid_alias(name):
            continue
        depth = p.count("/")
        by_name.setdefault(name, []).append(
            (
                (depth, p),
                DiscoveredSkill(name=name, source_path=source_dir, skill_md_path=p),
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


def index_repo(repo_alias: str) -> IndexResult:
    """Discover skills in a registered repo and write SkillIndex rows.

    Old rows for this repo are deleted before insertion (so renames/removals
    in the upstream repo are reflected on re-index).
    """
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(
            delete(SkillIndex).where(SkillIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        )
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
        haystack = " ".join(filter(None, [row.qualified_name, row.title, row.description])).lower()
        if q in haystack:
            out.append(row)
    return out
