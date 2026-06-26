"""Sub-agent discovery + search.

A sub-agent is any `AGENT.md` file inside a registered repo, or a flat
`<name>.md` file inside an `agents/` directory (anywhere in the repo).
The agent `name` is the directory containing `AGENT.md`, or the filename
stem for flat `agents/<name>.md` files. A bare `AGENT.md` at the repo root
uses the repo alias as its name.

If the same agent name appears at multiple locations, the shallower path
wins (ties broken by lexicographic path); the other is ignored.

Discovery results are persisted in the SQLite `AgentIndex` table. The index
is rebuilt from the cached bare clone's default ref on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from sqlmodel import delete, select

from aim.core import db, git, repos, validation
from aim.core.models import AgentIndex

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is required but be defensive
    yaml = None  # type: ignore[assignment]


def split_csv(value: str) -> list[str]:
    """Split a comma-separated string into a list of non-empty trimmed parts.

    Args:
        value: The comma-separated string to split.

    Returns:
        The list of trimmed, non-empty components.
    """
    return [p for p in (s.strip() for s in value.split(",")) if p]


_AGENT_RE = re.compile(
    r"^(?P<path>.*)/AGENT\.md$|^AGENT\.md$|"
    r"^(?:(?P<flat_prefix>.*)/)?agents/(?:[^/]+/)*(?P<name_flat>[^/]+)\.md$"
)


def _prefix_rank(path: str) -> int:
    """Rank a candidate path by prefix precedence (lower wins).

    Canonical locations win at the same depth; arbitrary paths are still
    discovered but ranked behind them.

    Args:
        path: The agent file path relative to the repo root.

    Returns:
        0 for top-level canonical paths, 1 for `.claude/agents/`, 2 otherwise.
    """
    if path.startswith("agents/") or path == "AGENT.md":
        return 0
    if path.startswith(".claude/agents/"):
        return 1
    return 2


class DiscoveredAgent(NamedTuple):
    """A single agent located during repo discovery."""

    name: str
    source_path: str  # path of the agent DIRECTORY relative to repo root
    agent_md_path: str  # path of the AGENT.md file relative to repo root


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering agents in a repo: those indexed and those shadowed."""

    repo_alias: str
    sha: str
    indexed: list[DiscoveredAgent]
    shadowed: list[DiscoveredAgent]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    """Discover all agents in a registered repo at its default ref.

    Resolves the repo's default ref, scans its tree for agent files, applies
    precedence rules when the same name appears multiple times, and reports
    both the winning agents and the shadowed duplicates.

    Args:
        repo_alias: The alias of the registered repo to scan.

    Returns:
        The index result holding the resolved sha, indexed agents, and
        shadowed duplicates.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    from aim.core import plugins  # lazy import avoids a module-load cycle

    plugin_dirs = plugins.owned_dir_prefixes(repo_alias, repo_dir, sha, paths)

    # Group candidates by agent name. Precedence: shallower path wins; at the
    # same depth, canonical prefixes (`agents/`, `.claude/agents/`) win over
    # arbitrary paths. Ties break by lexicographic path.
    by_name: dict[str, list[tuple[tuple[int, int, str], DiscoveredAgent]]] = {}
    for p in paths:
        match = _AGENT_RE.match(p)
        if not match:
            continue

        if not validation.is_safe_repo_path(p):
            continue
        if plugins.is_plugin_owned(p, plugin_dirs):
            continue  # bundled inside a plugin; not a standalone agent

        flat_name = match.group("name_flat")
        if flat_name is not None:
            name = flat_name
            source_dir = p
        else:
            path = match.group("path") or ""
            if path:
                name = Path(path).name
                source_dir = path
            else:
                name = repo_alias
                source_dir = ""

        if not validation.is_valid_agent_name(name):
            continue

        depth = p.count("/")
        by_name.setdefault(name, []).append(
            (
                (depth, _prefix_rank(p), p),
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


def _indexed_sha(repo_alias: str) -> str | None:
    """Return the SHA the repo's agents were last indexed at, or None if absent."""
    with db.session() as session:
        return session.exec(
            select(AgentIndex.indexed_at_sha)  # type: ignore[arg-type]
            .where(AgentIndex.repo_alias == repo_alias)
            .limit(1)
        ).first()


def index_repo(repo_alias: str) -> IndexResult:
    """Discover agents in a registered repo and persist AgentIndex rows.

    Skips the rebuild when the repo is already indexed at the current SHA.
    Otherwise it replaces any existing index rows for the repo with freshly
    discovered agents, reading every AGENT.md in one batched git process and
    parsing each agent's frontmatter for searchable metadata.

    Args:
        repo_alias: The alias of the registered repo to index.

    Returns:
        The discovery result describing what was indexed and shadowed.
    """
    result = discover(repo_alias)
    if _indexed_sha(repo_alias) == result.sha:
        return result
    repo_dir = repos.clone_dir(repo_alias)
    bodies = git.cat_files_text(
        repo_dir, result.sha, [agent.agent_md_path for agent in result.indexed]
    )
    with db.session() as session:
        session.exec(
            delete(AgentIndex).where(AgentIndex.repo_alias == repo_alias)  # type: ignore[arg-type]
        )
        for agent in result.indexed:
            body = bodies.get(agent.agent_md_path)
            title, description, tools, model = (
                _parse_agent_md(body) if body is not None else (None, None, [], None)
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
    """Parse leading YAML frontmatter from a document body.

    Args:
        body: The full document text, possibly starting with a `---` block.

    Returns:
        A tuple of the parsed frontmatter fields (empty if absent or
        unparseable) and the remaining body after the frontmatter.
    """
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
    body: str,
) -> tuple[str | None, str | None, list[str], str | None]:
    """Pull (name, description, tools, model) from an AGENT.md body.

    Frontmatter (YAML) is optional. Recognized keys: `name`, `description`,
    `tools`, `model` (and nested blocks like `mcpServers`, `skills` are ignored
    here because they are runtime hints for the agent, not search fields).
    """
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
    """Coerce a frontmatter value to a string, preserving None.

    Args:
        value: The raw frontmatter value of any type.

    Returns:
        The value as a string, or None if it was None.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    """Coerce a frontmatter value into a list of strings.

    Accepts a missing value, a single scalar, or an existing list.

    Args:
        value: The raw frontmatter value of any type.

    Returns:
        A list of string elements (empty if the value was None).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


class AgentNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the agent index."""


def index_row(qualified_name: str) -> AgentIndex:
    """Return the AgentIndex row for an indexed agent, or raise.

    Raises:
        AgentNotIndexedError: If no index row exists for the qualified name.
    """
    with db.session() as session:
        row = session.get(AgentIndex, qualified_name)
    if row is None:
        raise AgentNotIndexedError(qualified_name)
    return row


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
    """List indexed agents, optionally filtered to one repo.

    Args:
        repo_alias: If given, restrict results to this repo's agents.

    Returns:
        The matching agent index rows, sorted by qualified name.
    """
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
