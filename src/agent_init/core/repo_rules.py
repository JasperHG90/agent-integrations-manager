"""Rule discovery from registered source repos.

Rules live inside a registered repo at one of two fixed paths
(precedence: highest first):

    1. rules/<name>.md
    2. .claude/rules/<name>.md

If the same rule name appears at both locations, the higher-precedence one
wins and the other is ignored.

Discovery results are persisted in the SQLite `RuleIndex` table. The index
is rebuilt from the cached bare clone's default ref on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlmodel import delete, select

from agent_init.core import db, git, repos, validation
from agent_init.core.models import RuleIndex

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is required but be defensive
    yaml = None  # type: ignore[assignment]


_RULE_RE = re.compile(
    r"^(?:(?P<prefix>rules/|\.claude/rules/)(?P<name>[^/]+)\.md)$"
)


class DiscoveredRule(NamedTuple):
    name: str
    rule_md_path: str  # path of the .md file relative to repo root


@dataclass(frozen=True)
class IndexResult:
    repo_alias: str
    sha: str
    indexed: list[DiscoveredRule]
    shadowed: list[DiscoveredRule]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    by_name: dict[str, list[tuple[int, DiscoveredRule]]] = {}
    for p in paths:
        match = _RULE_RE.match(p)
        if not match:
            continue
        prefix = match.group("prefix") or ""
        name = match.group("name")
        if not validation.is_valid_rule_name(name):
            continue
        rank = 0 if prefix == "rules/" else 1
        by_name.setdefault(name, []).append(
            (rank, DiscoveredRule(name=name, rule_md_path=p))
        )

    indexed: list[DiscoveredRule] = []
    shadowed: list[DiscoveredRule] = []
    for _, candidates in sorted(by_name.items()):
        candidates.sort(key=lambda c: c[0])
        winner = candidates[0][1]
        indexed.append(winner)
        shadowed.extend(c[1] for c in candidates[1:])

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover rules in a registered repo and write RuleIndex rows."""
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(delete(RuleIndex).where(RuleIndex.repo_alias == repo_alias))  # type: ignore[arg-type]
        for rule in result.indexed:
            title, description = _parse_rule_md(repo_alias, result.sha, rule.rule_md_path)
            session.add(
                RuleIndex(
                    qualified_name=f"{repo_alias}/{rule.name}",
                    repo_alias=repo_alias,
                    rule_name=rule.name,
                    rule_md_path=rule.rule_md_path,
                    title=title,
                    description=description,
                    indexed_at_sha=result.sha,
                )
            )
        session.commit()
    return result


def _extract_frontmatter(body: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter if present; return (fields, remainder)."""
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


def _parse_rule_md(
    repo_alias: str, sha: str, path: str
) -> tuple[str | None, str | None]:
    """Pull (title, description) from a rule .md file.

    Frontmatter (YAML) is optional. Recognized keys: `title`, `name`, `description`.
    Falls back to first Markdown heading or first non-empty line.
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
        lines = remainder.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        if title is None:
            for line in lines:
                if line.strip():
                    title = line.strip()
                    break

    return title, description


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


class RuleNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the rule index."""


def read_rule_content(qualified_name: str) -> str:
    """Return the raw rule .md content for an indexed rule."""
    with db.session() as session:
        row = session.get(RuleIndex, qualified_name)
    if row is None:
        raise RuleNotIndexedError(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, row.rule_md_path)


def list_rules(repo_alias: str | None = None) -> list[RuleIndex]:
    with db.session() as session:
        stmt = select(RuleIndex)
        if repo_alias is not None:
            stmt = stmt.where(RuleIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str) -> list[RuleIndex]:
    """Case-insensitive substring search across qualified_name, title, description."""
    q = query.strip().lower()
    if not q:
        return list_rules()
    out: list[RuleIndex] = []
    for row in list_rules():
        haystack = " ".join(filter(None, [row.qualified_name, row.title, row.description])).lower()
        if q in haystack:
            out.append(row)
    return out

