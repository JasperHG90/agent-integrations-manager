"""Rule discovery from registered source repos.

A rule is any `.md` file whose stem is a valid rule name. The rule `name`
is the filename stem (no directory components). Common documentation names
like `README.md` are rejected because their stems are not valid rule names.

If the same rule name appears at multiple locations, the shallower path
wins (ties broken by lexicographic path); the other is ignored.

Discovery results are persisted in the SQLite `RuleIndex` table. The index
is rebuilt from the cached bare clone's default ref on refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlmodel import delete, select

from aim.core import db, git, repos, validation
from aim.core.models import RenderRule, RuleIndex

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is required but be defensive
    yaml = None  # type: ignore[assignment]


# Common documentation filenames that happen to have valid rule stems but
# should not be surfaced as installable rules.
_RULE_DENYLIST = {
    "readme",
    "license",
    "changelog",
    "contributing",
    "authors",
    "maintainers",
    "code_of_conduct",
    "conduct",
    "privacy",
    "terms",
    "support",
    "funding",
    "todo",
}

_RULE_RE = re.compile(r"^(?:(?P<path>.*)/)?(?P<name>[^/]+)\.md$")


# Only canonical rule locations are discovered. Arbitrary `*.md` elsewhere in a
# repo (docs, notes) must not surface as installable rules.
_RULE_PREFIXES = ("rules/", ".claude/rules/")


def _prefix_rank(path: str) -> int:
    """Return the precedence rank for a rule path's location prefix.

    Args:
        path: Rule `.md` path relative to the repo root.

    Returns:
        0 for `rules/`, 1 for `.claude/rules/` (lower wins).
    """
    if path.startswith("rules/"):
        return 0
    return 1  # ".claude/rules/"


class DiscoveredRule(NamedTuple):
    """A rule found in a repo: its name and source `.md` path."""

    name: str
    rule_md_path: str  # path of the .md file relative to repo root


@dataclass(frozen=True)
class IndexResult:
    """Outcome of discovering rules in a repo at a resolved SHA."""

    repo_alias: str
    sha: str
    indexed: list[DiscoveredRule]
    shadowed: list[DiscoveredRule]  # skipped duplicates at lower precedence


def discover(repo_alias: str) -> IndexResult:
    """Discover installable rules in a registered repo at its default ref.

    Args:
        repo_alias: Alias of the registered source repo to scan.

    Returns:
        An IndexResult with the winning rules and any shadowed duplicates.
    """
    repo = repos.get(repo_alias)
    repo_dir = repos.clone_dir(repo_alias)
    sha = git.get_backend().resolve_ref(repo_dir, repo.default_ref)
    paths = git.get_backend().ls_tree(repo_dir, sha)

    from aim.core import plugins  # lazy import avoids a module-load cycle

    plugin_dirs = plugins.owned_dir_prefixes(repo_alias, repo_dir, sha, paths)

    # Group candidates by rule name. Only `rules/` and `.claude/rules/` are
    # considered. Precedence: shallower path wins; at the same depth `rules/`
    # wins over `.claude/rules/`. Ties break by lexicographic path.
    by_name: dict[str, list[tuple[tuple[int, int, str], DiscoveredRule]]] = {}
    for p in paths:
        if not p.startswith(_RULE_PREFIXES):
            continue
        match = _RULE_RE.match(p)
        if not match:
            continue

        if not validation.is_safe_repo_path(p):
            continue
        if plugins.is_plugin_owned(p, plugin_dirs):
            continue  # bundled inside a plugin; not a standalone rule

        name = match.group("name")
        if not validation.is_valid_rule_name(name):
            continue
        if name.lower() in _RULE_DENYLIST:
            continue
        depth = p.count("/")
        by_name.setdefault(name, []).append(
            ((depth, _prefix_rank(p), p), DiscoveredRule(name=name, rule_md_path=p))
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
    """Discover rules in a registered repo and write RuleIndex rows.

    Args:
        repo_alias: Alias of the registered source repo to index.

    Returns:
        The IndexResult produced by discovery.
    """
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
    """Parse YAML frontmatter if present.

    Args:
        body: Raw Markdown file content.

    Returns:
        A tuple of the parsed frontmatter fields and the remaining body.
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


def _parse_rule_md(repo_alias: str, sha: str, path: str) -> tuple[str | None, str | None]:
    """Pull a title and description from a rule `.md` file.

    Frontmatter (YAML) is optional. Recognized keys: `title`, `name`, `description`.
    Falls back to the first Markdown heading or first non-empty line for the title.

    Args:
        repo_alias: Alias of the repo containing the rule.
        sha: Resolved commit SHA to read the file at.
        path: Path of the rule `.md` file relative to the repo root.

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
    """Coerce a frontmatter value to a string, preserving None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


class RuleNotIndexedError(KeyError):
    """The requested qualified_name doesn't appear in the rule index."""


def render_rule(installed: object) -> RenderRule:
    """Build the AGENTS.md render view of an installed rule.

    The body is read at the rule's pinned SHA. Frontmatter (if any) is stripped
    from the rendered body and its `description`/`title` surfaced separately.

    Args:
        installed: The InstalledRule to render (typed as object for late import).

    Returns:
        A RenderRule with the rule name, body, and optional description.
    """
    from aim.core.models import InstalledRule

    assert isinstance(installed, InstalledRule)
    repo_dir = repos.clone_dir(installed.repo_alias)
    raw = git.get_backend().cat_file(repo_dir, installed.current.sha, installed.source_path)
    frontmatter, body = _extract_frontmatter(raw)
    description = _as_str(frontmatter.get("description") or frontmatter.get("title"))
    name = installed.qualified_name.split("/", 1)[-1]
    return RenderRule(name=name, body=body, description=description)


def index_row(qualified_name: str) -> RuleIndex:
    """Return the RuleIndex row for an indexed rule, or raise."""
    with db.session() as session:
        row = session.get(RuleIndex, qualified_name)
    if row is None:
        raise RuleNotIndexedError(qualified_name)
    return row


def read_rule_content(qualified_name: str) -> str:
    """Return the raw rule .md content for an indexed rule."""
    with db.session() as session:
        row = session.get(RuleIndex, qualified_name)
    if row is None:
        raise RuleNotIndexedError(qualified_name)
    repo_dir = repos.clone_dir(row.repo_alias)
    return git.get_backend().cat_file(repo_dir, row.indexed_at_sha, row.rule_md_path)


def list_rules(repo_alias: str | None = None) -> list[RuleIndex]:
    """Return indexed rules sorted by qualified name, optionally filtered by repo.

    Args:
        repo_alias: If given, restrict results to this repo's rules.

    Returns:
        The matching RuleIndex rows, sorted by qualified name.
    """
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
