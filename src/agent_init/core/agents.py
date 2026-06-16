"""Sub-agent discovery + search.

A sub-agent lives inside a registered repo at any depth under one of these
prefixes (precedence: highest first):

    1. agents/.../<name>/AGENT.md or agents/.../<name>.md
    2. .claude/agents/.../<name>/AGENT.md or .claude/agents/.../<name>.md

Intermediate directories under `agents/` and `.claude/agents/` are allowed
to support repos that group agents by category. The agent `name` is the
parent directory name (nested form) or the filename stem (flat form).

If the same agent name appears at multiple locations, the higher-precedence
one wins (ties broken by shallower path); the other is ignored.

Discovery results are persisted in the SQLite `AgentIndex` table. The index
is rebuilt from the cached bare clone's default ref on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlmodel import delete, select

from agent_init.core import db, git, repos, validation
from agent_init.core.models import AgentIndex

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is required but be defensive
    yaml = None  # type: ignore[assignment]


def split_csv(value: str) -> list[str]:
    """Helper to read a CSV field back into a list."""
    return [p for p in (s.strip() for s in value.split(",")) if p]


_AGENT_RE = re.compile(
    r"^(?P<prefix>agents/|\.claude/agents/)(?:[^/]+/)*?"
    r"(?:(?P<name_dir>[^/]+)/AGENT\.md|(?P<name_flat>[^/]+)\.md)$"
)


class DiscoveredAgent(NamedTuple):
    name: str
    source_path: str  # path of the agent DIRECTORY relative to repo root
    agent_md_path: str  # path of the AGENT.md file relative to repo root


@dataclass(frozen=True)
class IndexResult:
    repo_alias: str
    sha: str
    indexed: list[DiscoveredAgent]
    shadowed: list[DiscoveredAgent]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    by_name: dict[str, list[tuple[tuple[int, int, str], DiscoveredAgent]]] = {}
    for p in paths:
        match = _AGENT_RE.match(p)
        if not match:
            continue
        prefix = match.group("prefix") or ""
        name = match.group("name_dir") or match.group("name_flat")
        if not validation.is_valid_agent_name(name):
            continue
        prefix_rank = 0 if prefix == "agents/" else 1
        depth = p.count("/")
        # Flat file: agents/.../<name>.md  -> source_path is the file itself.
        # Nested dir: agents/.../<name>/AGENT.md -> source_path is the directory.
        if p.endswith(f"/{name}.md") and not p.endswith(f"/{name}/AGENT.md"):
            source_dir = p
        else:
            source_dir = p[: -len("/AGENT.md")]
        by_name.setdefault(name, []).append(
            (
                (prefix_rank, depth, p),
                DiscoveredAgent(name=name, source_path=source_dir, agent_md_path=p),
            )
        )

    indexed: list[DiscoveredAgent] = []
    shadowed: list[DiscoveredAgent] = []
    for _, candidates in sorted(by_name.items()):
        candidates.sort(key=lambda c: c[0])
        winner = candidates[0][1]
        indexed.append(winner)
        shadowed.extend(c[1] for c in candidates[1:])

    return IndexResult(repo_alias=repo_alias, sha=sha, indexed=indexed, shadowed=shadowed)


def index_repo(repo_alias: str) -> IndexResult:
    """Discover agents in a registered repo and write AgentIndex rows."""
    result = discover(repo_alias)
    with db.session() as session:
        session.exec(delete(AgentIndex).where(AgentIndex.repo_alias == repo_alias))
        for agent in result.indexed:
            title, description, tools, model = _parse_agent_md(
                repo_alias, result.sha, agent.agent_md_path
            )
            session.add(
                AgentIndex(
                    qualified_name=f"{repo_alias}/{agent.name}",
                    repo_alias=repo_alias,
                    agent_name=agent.name,
                    source_path=agent.source_path,
                    agent_md_path=agent.agent_md_path,
                    title=title,
                    description=description,
                    indexed_at_sha=result.sha,
                    tools=",".join(tools),
                    model=model,
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


def _parse_agent_md(
    repo_alias: str, sha: str, path: str
) -> tuple[str | None, str | None, list[str], str | None]:
    """Pull (name, description, tools, model) from an AGENT.md.

    Frontmatter (YAML) is optional. Recognized keys: `name`, `description`,
    `tools`, `model` (and nested blocks like `mcpServers`, `skills` are ignored
    here because they are runtime hints for the agent, not search fields).
    """
    repo_dir = repos.clone_dir(repo_alias)
    try:
        body = git.get_backend().cat_file(repo_dir, sha, path)
    except git.GitError:
        return None, None, [], None

    frontmatter, remainder = _extract_frontmatter(body)
    title = _as_str(frontmatter.get("name"))
    description = _as_str(frontmatter.get("description"))
    tools = _as_str_list(frontmatter.get("tools"))
    model = _as_str(frontmatter.get("model"))

    if title is None:
        # Fall back to first Markdown heading, then first non-empty line.
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

    return title, description, tools, model


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


class AgentNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the agent index."""


def read_agent_content(qualified_name: str) -> str:
    """Return the raw AGENT.md bytes for an indexed agent."""
    with db.session() as session:
        row = session.get(AgentIndex, qualified_name)
    if row is None:
        raise AgentNotIndexedError(qualified_name)
    if not row.agent_md_path:
        raise AgentNotIndexedError(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, row.agent_md_path)


def list_agents(repo_alias: str | None = None) -> list[AgentIndex]:
    with db.session() as session:
        stmt = select(AgentIndex)
        if repo_alias is not None:
            stmt = stmt.where(AgentIndex.repo_alias == repo_alias)
        rows = list(session.exec(stmt).all())
    rows.sort(key=lambda r: r.qualified_name)
    return rows


def search(query: str) -> list[AgentIndex]:
    """Case-insensitive substring search across qualified_name, title, description, tools."""
    q = query.strip().lower()
    if not q:
        return list_agents()
    out: list[AgentIndex] = []
    for row in list_agents():
        haystack = " ".join(
            filter(
                None,
                [
                    row.qualified_name,
                    row.title,
                    row.description,
                    row.tools,
                    row.model,
                ],
            )
        ).lower()
        if q in haystack:
            out.append(row)
    return out
