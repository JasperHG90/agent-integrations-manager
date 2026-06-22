"""Skill source repo registry. Globally registered repos live in the SQLite
DB; their bare clones live in `user_cache_dir/repos/<alias>/`.

Operations:
- `add(alias, url)`            — register + bare-clone.
- `list_repos()`               — list registered repos.
- `get(alias)`                 — fetch one.
- `remove(alias)`              — unregister and remove the clone.
- `rename(old, new)`           — rename alias and move the clone dir.
- `refresh(alias)`             — `git fetch --tags --prune`.

The plan calls out that we never `git checkout` against the bare cache clone;
all read operations use `git -C` against the bare repo.
"""

from __future__ import annotations

import importlib
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import select

from aim.core import content_guard, db, git, paths
from aim.core.models import (
    AgentIndex,
    ArchetypeIndex,
    RegisteredRepo,
    RuleIndex,
    SkillIndex,
    TemplateIndex,
)

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Substrings git emits when it cannot authenticate to a remote. Used to turn
# low-level git errors into actionable messages for consultants juggling
# multiple gh accounts / GitHub Enterprise hosts.
_AUTH_HINTS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "repository not found",
    "remote: invalid username or password",
    "remote: permission denied",
    "remote: access denied",
    "http basic: access denied",
    "denied to",
    "403",
    "401",
    "terminal prompts disabled",
)


def _looks_like_auth_failure(stderr: str) -> bool:
    """Return whether git stderr indicates an authentication/access failure.

    Args:
        stderr: The captured stderr text from a failed git command.
    """
    text = stderr.lower()
    return any(hint in text for hint in _AUTH_HINTS)


class RepoAliasError(ValueError):
    """Raised when a repo alias fails validation."""


class RepoExistsError(ValueError):
    """Raised when registering an alias that is already registered."""


class RepoNotFoundError(KeyError):
    """Raised when a requested repo alias is not registered."""


class RepoHasNoArtifactsError(ValueError):
    """Raised when a registered repo contains no discoverable skills or agents."""


# Backward-compatible alias for existing callers/tests.
RepoHasNoSkillsError = RepoHasNoArtifactsError


def _validate_alias(alias: str) -> None:
    """Validate that an alias is lowercase alphanumeric with `_`/`-` only.

    Args:
        alias: The candidate repo alias.

    Raises:
        RepoAliasError: If the alias does not match the allowed pattern.
    """
    if not _ALIAS_RE.fullmatch(alias):
        raise RepoAliasError(
            f"repo alias {alias!r} invalid: must be lowercase alphanumeric, _, or -"
        )


def clone_dir(alias: str) -> Path:
    """Return the bare-clone cache directory for a repo alias."""
    return paths.repos_cache_dir() / alias


def _auth_help(alias: str, url: str, original: str) -> str:
    """Build an actionable authentication-failure message for a repo.

    Args:
        alias: The repo alias being accessed.
        url: The remote URL that failed.
        original: The underlying git error text to surface verbatim.

    Returns:
        A multi-line hint suggesting `gh auth` commands, with a `GH_HOST`
        hint when the host looks like a GitHub Enterprise instance.
    """
    host = _host_from_url(url)
    enterprise_hint = (
        f" (GH_HOST={host})" if host and ".github.com" not in host and host != "github.com" else ""
    )
    return (
        f"{alias}: failed to access {url}: {original}\n"
        f"The remote rejected the request, probably because the active git/gh credentials "
        f"don't have access.\n"
        f"Check:  gh auth status{enterprise_hint}\n"
        f"Switch:   gh auth switch{enterprise_hint}\n"
        f"If git isn't using gh yet, run:  gh auth setup-git{enterprise_hint}"
    )


def _host_from_url(url: str) -> str | None:
    """Extract a hostname from https or ssh git URLs."""
    if url.startswith("https://") or url.startswith("http://"):
        rest = url.split("://", 1)[1]
        return rest.split("/", 1)[0].split(":", 1)[0] or None
    if url.startswith("git@") and ":" in url:
        return url.split(":", 1)[0].split("@", 1)[-1] or None
    return None


def _wrap_git_error(alias: str, url: str, exc: git.GitError) -> git.GitError:
    """Translate an auth-related git error into an actionable one.

    Args:
        alias: The repo alias the operation targeted.
        url: The remote URL involved.
        exc: The original git error.

    Returns:
        A `GitError` with auth guidance when the failure looks like an
        authentication problem; otherwise the original exception unchanged.
    """
    stderr = str(exc)
    if _looks_like_auth_failure(stderr):
        return git.GitError(_auth_help(alias, url, stderr))
    return exc


def add(
    alias: str,
    url: str,
    *,
    default_ref: str = "HEAD",
    allow_empty: bool = False,
    allow_insecure: bool = False,
) -> RegisteredRepo:
    """Register a skill source repo, bare-mirror-clone it, and index its artifacts.

    Args:
        alias: The alias to register the repo under.
        url: The remote git URL to clone.
        default_ref: The ref to resolve and track for this repo.
        allow_empty: Keep the registration even if no artifacts are found.
        allow_insecure: Permit insecure (non-https) URLs.

    Returns:
        The newly registered repo record.

    Raises:
        RepoExistsError: If the alias is already registered.
        RepoHasNoArtifactsError: If no skills, agents, or rules are found and
            `allow_empty` is False; the clone is removed so registration
            leaves no state behind.
    """
    _validate_alias(alias)
    content_guard.require_secure_url(url, allow_insecure=allow_insecure)
    # Repo governance is enforced per-project (at lock and at install), where the
    # project's aim.toml policy is in scope — not here, since registering a repo is
    # a global cache operation with no project context.
    paths.ensure_global_dirs()
    with db.session() as session:
        existing = session.get(RegisteredRepo, alias)
        if existing is not None:
            raise RepoExistsError(alias)
    dest = clone_dir(alias)
    try:
        git.get_backend().clone_bare(url, dest)
    except git.GitError as exc:
        raise _wrap_git_error(alias, url, exc) from exc
    try:
        head_sha = git.get_backend().resolve_ref(dest, default_ref)
    except git.GitError:
        head_sha = None
    repo = RegisteredRepo(
        alias=alias,
        url=url,
        default_ref=default_ref,
        last_fetched_at=datetime.now(UTC),
        last_sha=head_sha,
    )
    with db.session() as session:
        session.add(repo)
        session.commit()
        session.refresh(repo)

    # Lazy imports to break circular deps.
    _skills = importlib.import_module("aim.core.skills")
    _agents = importlib.import_module("aim.core.agents")
    _repo_rules = importlib.import_module("aim.core.repo_rules")
    _archetypes = importlib.import_module("aim.core.archetypes")
    _repo_templates = importlib.import_module("aim.core.repo_templates")

    try:
        skill_result = _skills.index_repo(alias)
        agent_result = _agents.index_repo(alias)
        rule_result = _repo_rules.index_repo(alias)
        archetype_result = _archetypes.index_repo(alias)
        template_result = _repo_templates.index_repo(alias)
    except Exception:
        # Roll back the registration so the next attempt isn't blocked.
        remove(alias)
        raise
    if (
        not skill_result.indexed
        and not agent_result.indexed
        and not rule_result.indexed
        and not archetype_result.indexed
        and not template_result.indexed
        and not allow_empty
    ):
        remove(alias)
        raise RepoHasNoArtifactsError(
            f"{alias}: no SKILL.md, AGENT.md, rule .md, instruction archetype, or "
            f"project template found anywhere in the repository"
        )
    return repo


def list_repos() -> list[RegisteredRepo]:
    """Return all registered repos sorted by alias."""
    with db.session() as session:
        rows = list(session.exec(select(RegisteredRepo)).all())
    rows.sort(key=lambda r: r.alias)
    return rows


def artifact_kinds(alias: str) -> set[str]:
    """Return which artifact types a repo contains: skill, agent, rules, archetype."""
    kinds: set[str] = set()
    with db.session() as session:
        if session.exec(select(SkillIndex).where(SkillIndex.repo_alias == alias).limit(1)).first():  # type: ignore[arg-type]
            kinds.add("skill")
        if session.exec(select(AgentIndex).where(AgentIndex.repo_alias == alias).limit(1)).first():  # type: ignore[arg-type]
            kinds.add("agent")
        if session.exec(select(RuleIndex).where(RuleIndex.repo_alias == alias).limit(1)).first():  # type: ignore[arg-type]
            kinds.add("rules")
        if session.exec(
            select(ArchetypeIndex).where(ArchetypeIndex.repo_alias == alias).limit(1)
        ).first():  # type: ignore[arg-type]
            kinds.add("archetype")
        if session.exec(
            select(TemplateIndex).where(TemplateIndex.repo_alias == alias).limit(1)
        ).first():  # type: ignore[arg-type]
            kinds.add("template")
    return kinds


def get(alias: str) -> RegisteredRepo:
    """Return the registered repo for an alias.

    Raises:
        RepoNotFoundError: If the alias is not registered.
    """
    with db.session() as session:
        row = session.get(RegisteredRepo, alias)
    if row is None:
        raise RepoNotFoundError(alias)
    return row


def remove(alias: str) -> None:
    """Unregister a repo, drop its index rows, and remove the bare clone.

    Raises:
        RepoNotFoundError: If the alias is not registered.
    """
    with db.session() as session:
        row = session.get(RegisteredRepo, alias)
        if row is None:
            raise RepoNotFoundError(alias)
        session.exec(_delete_skill_index(alias))
        session.exec(_delete_agent_index(alias))
        session.exec(_delete_rule_index(alias))
        session.exec(_delete_archetype_index(alias))
        session.exec(_delete_template_index(alias))
        session.delete(row)
        session.commit()
    git.remove_clone(clone_dir(alias))


def project_artifacts_for_repo(project_root: Path, alias: str) -> list[str]:
    """Return the qualified names of a project's artifacts that come from a repo.

    Read-only; used to warn after a (global) repo removal that a project still
    declares artifacts sourced from the now-unregistered repo. Removing a global
    repo registration is a reversible cache eviction — `sync` re-registers from the
    lockfile — so the project's declarations are deliberately left untouched.

    Args:
        project_root: The project directory whose aim.toml is inspected.
        alias: The repo alias to match declared artifacts against.

    Returns:
        The qualified names declared from `alias`; empty when there are no
        declarations.
    """
    from aim.core import declarations

    try:
        decl = declarations.load(project_root)
    except declarations.DeclarationsNotFoundError:
        return []
    return (
        [s.qualified_name for s in decl.skills if s.repo_alias == alias]
        + [a.qualified_name for a in decl.agents if a.repo_alias == alias]
        + [r.qualified_name for r in decl.rules if r.repo_alias == alias]
    )


def _delete_skill_index(alias: str):  # type: ignore[no-untyped-def]
    """Build a delete statement for a repo's skill index rows."""
    from sqlmodel import delete as _delete

    return _delete(SkillIndex).where(SkillIndex.repo_alias == alias)  # type: ignore[arg-type]


def _delete_agent_index(alias: str):  # type: ignore[no-untyped-def]
    """Build a delete statement for a repo's agent index rows."""
    from sqlmodel import delete as _delete

    from aim.core.models import AgentIndex as _AgentIndex

    return _delete(_AgentIndex).where(_AgentIndex.repo_alias == alias)  # type: ignore[arg-type]


def _delete_rule_index(alias: str):  # type: ignore[no-untyped-def]
    """Build a delete statement for a repo's rule index rows."""
    from sqlmodel import delete as _delete

    from aim.core.models import RuleIndex as _RuleIndex

    return _delete(_RuleIndex).where(_RuleIndex.repo_alias == alias)  # type: ignore[arg-type]


def _delete_archetype_index(alias: str):  # type: ignore[no-untyped-def]
    """Build a delete statement for a repo's archetype index rows."""
    from sqlmodel import delete as _delete

    from aim.core.models import ArchetypeIndex as _ArchetypeIndex

    return _delete(_ArchetypeIndex).where(_ArchetypeIndex.repo_alias == alias)  # type: ignore[arg-type]


def _delete_template_index(alias: str):  # type: ignore[no-untyped-def]
    """Build a delete statement for a repo's template index rows."""
    from sqlmodel import delete as _delete

    from aim.core.models import TemplateIndex as _TemplateIndex

    return _delete(_TemplateIndex).where(_TemplateIndex.repo_alias == alias)  # type: ignore[arg-type]


def rename(old: str, new: str) -> RegisteredRepo:
    """Rename a repo alias, moving its index rows and bare clone directory.

    Args:
        old: The current alias.
        new: The new alias.

    Returns:
        The renamed repo record (or the existing one if `old == new`).

    Raises:
        RepoNotFoundError: If `old` is not registered.
        RepoExistsError: If `new` is already registered.
    """
    _validate_alias(new)
    if old == new:
        return get(old)
    # Commit the DB rename first; if that succeeds, move the clone dir. If
    # the dir move fails (cross-device, permissions), best-effort: undo the
    # DB change so we stay consistent.
    with db.session() as session:
        existing = session.get(RegisteredRepo, old)
        if existing is None:
            raise RepoNotFoundError(old)
        if session.get(RegisteredRepo, new) is not None:
            raise RepoExistsError(new)
        renamed = RegisteredRepo(
            alias=new,
            url=existing.url,
            default_ref=existing.default_ref,
            last_fetched_at=existing.last_fetched_at,
            last_sha=existing.last_sha,
        )
        session.delete(existing)
        session.add(renamed)
        # Move skill and agent index rows too.
        for row in list(session.exec(select(SkillIndex).where(SkillIndex.repo_alias == old)).all()):  # type: ignore[arg-type]
            new_qn = f"{new}/{row.skill_name}"
            session.add(
                SkillIndex(
                    qualified_name=new_qn,
                    repo_alias=new,
                    skill_name=row.skill_name,
                    source_path=row.source_path,
                    skill_md_path=row.skill_md_path,
                    title=row.title,
                    description=row.description,
                    indexed_at_sha=row.indexed_at_sha,
                    prereqs=row.prereqs,
                    provides=row.provides,
                )
            )
            session.delete(row)
        for row in list(session.exec(select(AgentIndex).where(AgentIndex.repo_alias == old)).all()):  # type: ignore[arg-type]
            new_qn = f"{new}/{row.agent_name}"
            session.add(
                AgentIndex(
                    qualified_name=new_qn,
                    repo_alias=new,
                    agent_name=row.agent_name,
                    source_path=row.source_path,
                    agent_md_path=row.agent_md_path,
                    title=row.title,
                    description=row.description,
                    indexed_at_sha=row.indexed_at_sha,
                    tools=row.tools,
                    model=row.model,
                )
            )
            session.delete(row)
        for row in list(session.exec(select(RuleIndex).where(RuleIndex.repo_alias == old)).all()):  # type: ignore[arg-type]
            new_qn = f"{new}/{row.rule_name}"
            session.add(
                RuleIndex(
                    qualified_name=new_qn,
                    repo_alias=new,
                    rule_name=row.rule_name,
                    rule_md_path=row.rule_md_path,
                    title=row.title,
                    description=row.description,
                    indexed_at_sha=row.indexed_at_sha,
                )
            )
            session.delete(row)
        for row in list(  # type: ignore[assignment]
            session.exec(select(ArchetypeIndex).where(ArchetypeIndex.repo_alias == old)).all()  # type: ignore[arg-type]
        ):
            session.add(
                ArchetypeIndex(
                    qualified_name=f"{new}/{row.archetype_name}",
                    repo_alias=new,
                    archetype_name=row.archetype_name,
                    source_path=row.source_path,
                    instruction_path=row.instruction_path,
                    available=row.available,
                    title=row.title,
                    description=row.description,
                    indexed_at_sha=row.indexed_at_sha,
                )
            )
            session.delete(row)
        for row in list(  # type: ignore[assignment]
            session.exec(select(TemplateIndex).where(TemplateIndex.repo_alias == old)).all()  # type: ignore[arg-type]
        ):
            session.add(
                TemplateIndex(
                    qualified_name=f"{new}/{row.template_name}",
                    repo_alias=new,
                    template_name=row.template_name,
                    template_toml_path=row.template_toml_path,
                    title=row.title,
                    description=row.description,
                    indexed_at_sha=row.indexed_at_sha,
                )
            )
            session.delete(row)
        session.commit()

    old_dir = clone_dir(old)
    new_dir = clone_dir(new)
    if old_dir.exists():
        try:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
        except OSError:
            # Roll back the DB rename.
            with db.session() as session:
                cur = session.get(RegisteredRepo, new)
                if cur is not None:
                    restored = RegisteredRepo(
                        alias=old,
                        url=cur.url,
                        default_ref=cur.default_ref,
                        last_fetched_at=cur.last_fetched_at,
                        last_sha=cur.last_sha,
                    )
                    session.delete(cur)
                    session.add(restored)
                    for row in list(
                        session.exec(select(SkillIndex).where(SkillIndex.repo_alias == new)).all()
                    ):  # type: ignore[arg-type]
                        session.add(
                            SkillIndex(
                                qualified_name=f"{old}/{row.skill_name}",
                                repo_alias=old,
                                skill_name=row.skill_name,
                                source_path=row.source_path,
                                skill_md_path=row.skill_md_path,
                                title=row.title,
                                description=row.description,
                                indexed_at_sha=row.indexed_at_sha,
                                prereqs=row.prereqs,
                                provides=row.provides,
                            )
                        )
                        session.delete(row)
                    for row in list(
                        session.exec(select(AgentIndex).where(AgentIndex.repo_alias == new)).all()
                    ):  # type: ignore[arg-type]
                        session.add(
                            AgentIndex(
                                qualified_name=f"{old}/{row.agent_name}",
                                repo_alias=old,
                                agent_name=row.agent_name,
                                source_path=row.source_path,
                                agent_md_path=row.agent_md_path,
                                title=row.title,
                                description=row.description,
                                indexed_at_sha=row.indexed_at_sha,
                                tools=row.tools,
                                model=row.model,
                            )
                        )
                        session.delete(row)
                    for row in list(
                        session.exec(select(RuleIndex).where(RuleIndex.repo_alias == new)).all()
                    ):  # type: ignore[arg-type]
                        session.add(
                            RuleIndex(
                                qualified_name=f"{old}/{row.rule_name}",
                                repo_alias=old,
                                rule_name=row.rule_name,
                                rule_md_path=row.rule_md_path,
                                title=row.title,
                                description=row.description,
                                indexed_at_sha=row.indexed_at_sha,
                            )
                        )
                        session.delete(row)
                    for row in list(  # type: ignore[assignment]
                        session.exec(
                            select(ArchetypeIndex).where(ArchetypeIndex.repo_alias == new)  # type: ignore[arg-type]
                        ).all()
                    ):
                        session.add(
                            ArchetypeIndex(
                                qualified_name=f"{old}/{row.archetype_name}",
                                repo_alias=old,
                                archetype_name=row.archetype_name,
                                source_path=row.source_path,
                                instruction_path=row.instruction_path,
                                available=row.available,
                                title=row.title,
                                description=row.description,
                                indexed_at_sha=row.indexed_at_sha,
                            )
                        )
                        session.delete(row)
                    for row in list(  # type: ignore[assignment]
                        session.exec(
                            select(TemplateIndex).where(TemplateIndex.repo_alias == new)  # type: ignore[arg-type]
                        ).all()
                    ):
                        session.add(
                            TemplateIndex(
                                qualified_name=f"{old}/{row.template_name}",
                                repo_alias=old,
                                template_name=row.template_name,
                                template_toml_path=row.template_toml_path,
                                title=row.title,
                                description=row.description,
                                indexed_at_sha=row.indexed_at_sha,
                            )
                        )
                        session.delete(row)
                    session.commit()
            raise
    return get(new)


class RefDisappearedError(RuntimeError):
    """default_ref no longer resolves on the remote (branch deleted, etc.)."""


def _fetch(alias: str, *, allow_insecure: bool = False) -> None:
    """Fetch a repo's remote into its bare clone (network only, no DB writes).

    Safe to run concurrently across repos: it touches only the per-alias clone and
    a read of the repo registry.

    Raises:
        git.GitError: The fetch failed (wrapped with auth guidance when relevant).
    """
    row = get(alias)
    content_guard.require_secure_url(row.url, allow_insecure=allow_insecure)
    try:
        git.get_backend().fetch(clone_dir(alias))
    except git.GitError as exc:
        raise _wrap_git_error(alias, row.url, exc) from exc


def _resolve_and_reindex(alias: str, *, previous_sha: str | None) -> RegisteredRepo:
    """Resolve the post-fetch SHA, persist it, and reindex when it changed.

    Does the DB writes for a refresh, so callers run this serially to avoid SQLite
    write contention.

    Raises:
        RefDisappearedError: If `default_ref` no longer resolves upstream.
    """
    repo_dir = clone_dir(alias)
    default_ref = get(alias).default_ref
    ref_missing = False
    try:
        new_sha: str | None = git.get_backend().resolve_ref(repo_dir, default_ref)
    except git.GitError:
        new_sha = None
        ref_missing = True

    with db.session() as session:
        fresh = session.get(RegisteredRepo, alias)
        if fresh is None:  # pragma: no cover — concurrent delete
            raise RepoNotFoundError(alias)
        fresh.last_fetched_at = datetime.now(UTC)
        fresh.last_sha = new_sha
        session.add(fresh)
        session.commit()
        session.refresh(fresh)

    if ref_missing:
        raise RefDisappearedError(
            f"{alias}: default ref {default_ref!r} no longer resolves upstream"
        )

    if new_sha != previous_sha:
        _skills = importlib.import_module("aim.core.skills")
        _agents = importlib.import_module("aim.core.agents")
        _repo_rules = importlib.import_module("aim.core.repo_rules")
        _archetypes = importlib.import_module("aim.core.archetypes")
        _repo_templates = importlib.import_module("aim.core.repo_templates")

        _skills.index_repo(alias)
        _agents.index_repo(alias)
        _repo_rules.index_repo(alias)
        _archetypes.index_repo(alias)
        _repo_templates.index_repo(alias)
    return fresh


def refresh(alias: str, *, allow_insecure: bool = False) -> RegisteredRepo:
    """Fetch a repo's remote, update its tracked SHA, and reindex on change.

    Args:
        alias: The repo alias to refresh.
        allow_insecure: Permit insecure (non-https) URLs.

    Returns:
        The updated repo record.

    Raises:
        RefDisappearedError: If `default_ref` no longer resolves upstream.
    """
    previous_sha = get(alias).last_sha
    _fetch(alias, allow_insecure=allow_insecure)
    return _resolve_and_reindex(alias, previous_sha=previous_sha)


def refresh_many(
    aliases: list[str], *, allow_insecure: bool = False
) -> list[tuple[str, RegisteredRepo | None, Exception | None]]:
    """Refresh several repos, fetching all of them in parallel.

    Network fetches (the slow part) run concurrently in a thread pool; the DB
    resolve/reindex step then runs serially per repo to avoid SQLite write
    contention. Per-repo failures are returned, not raised, so one bad repo does
    not abort the rest.

    Args:
        aliases: Repo aliases to refresh.
        allow_insecure: Permit insecure (non-https) URLs.

    Returns:
        One ``(alias, repo_or_None, error_or_None)`` tuple per alias, in input order.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not aliases:
        return []
    previous = {alias: get(alias).last_sha for alias in aliases}

    def _try_fetch(alias: str) -> tuple[str, Exception | None]:
        try:
            _fetch(alias, allow_insecure=allow_insecure)
        except Exception as exc:
            return (alias, exc)
        return (alias, None)

    fetch_errors: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(aliases))) as pool:
        for alias, err in pool.map(_try_fetch, aliases):
            if err is not None:
                fetch_errors[alias] = err

    results: list[tuple[str, RegisteredRepo | None, Exception | None]] = []
    for alias in aliases:
        if alias in fetch_errors:
            results.append((alias, None, fetch_errors[alias]))
            continue
        try:
            results.append((alias, _resolve_and_reindex(alias, previous_sha=previous[alias]), None))
        except Exception as exc:
            results.append((alias, None, exc))
    return results
