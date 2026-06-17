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
from aim.core.models import AgentIndex, RegisteredRepo, RuleIndex, SkillIndex

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
    text = stderr.lower()
    return any(hint in text for hint in _AUTH_HINTS)


class RepoAliasError(ValueError):
    pass


class RepoExistsError(ValueError):
    pass


class RepoNotFoundError(KeyError):
    pass


class RepoHasNoArtifactsError(ValueError):
    """Raised when a registered repo contains no discoverable skills or agents."""


# Backward-compatible alias for existing callers/tests.
RepoHasNoSkillsError = RepoHasNoArtifactsError


def _validate_alias(alias: str) -> None:
    if not _ALIAS_RE.fullmatch(alias):
        raise RepoAliasError(
            f"repo alias {alias!r} invalid: must be lowercase alphanumeric, _, or -"
        )


def clone_dir(alias: str) -> Path:
    return paths.repos_cache_dir() / alias


def _auth_help(alias: str, url: str, original: str) -> str:
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
        # https://host/path
        rest = url.split("://", 1)[1]
        return rest.split("/", 1)[0].split(":", 1)[0] or None
    if url.startswith("git@") and ":" in url:
        # git@host:path
        return url.split(":", 1)[0].split("@", 1)[-1] or None
    return None


def _wrap_git_error(alias: str, url: str, exc: git.GitError) -> git.GitError:
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
    """Register a skill source repo, bare-mirror-clone it, and index its skills.

    If the repo contains no discoverable skills and `allow_empty=False`, the
    clone is removed and `RepoHasNoSkillsError` is raised — registration
    leaves no state behind.
    """
    _validate_alias(alias)
    content_guard.require_secure_url(url, allow_insecure=allow_insecure)
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

    # Index now. Lazy imports to break circular deps.
    _skills = importlib.import_module("aim.core.skills")
    _agents = importlib.import_module("aim.core.agents")
    _repo_rules = importlib.import_module("aim.core.repo_rules")

    try:
        skill_result = _skills.index_repo(alias)
        agent_result = _agents.index_repo(alias)
        rule_result = _repo_rules.index_repo(alias)
    except Exception:
        # Roll back the registration so the next attempt isn't blocked.
        remove(alias)
        raise
    if (
        not skill_result.indexed
        and not agent_result.indexed
        and not rule_result.indexed
        and not allow_empty
    ):
        remove(alias)
        raise RepoHasNoArtifactsError(
            f"{alias}: no SKILL.md, AGENT.md, or valid rule .md files found "
            f"anywhere in the repository"
        )
    return repo


def list_repos() -> list[RegisteredRepo]:
    with db.session() as session:
        rows = list(session.exec(select(RegisteredRepo)).all())
    rows.sort(key=lambda r: r.alias)
    return rows


def artifact_kinds(alias: str) -> set[str]:
    """Return which artifact types a repo contains: skill, agent, rules."""
    kinds: set[str] = set()
    with db.session() as session:
        if session.exec(select(SkillIndex).where(SkillIndex.repo_alias == alias).limit(1)).first():  # type: ignore[arg-type]
            kinds.add("skill")
        if session.exec(select(AgentIndex).where(AgentIndex.repo_alias == alias).limit(1)).first():  # type: ignore[arg-type]
            kinds.add("agent")
        if session.exec(select(RuleIndex).where(RuleIndex.repo_alias == alias).limit(1)).first():  # type: ignore[arg-type]
            kinds.add("rules")
    return kinds


def get(alias: str) -> RegisteredRepo:
    with db.session() as session:
        row = session.get(RegisteredRepo, alias)
    if row is None:
        raise RepoNotFoundError(alias)
    return row


def remove(alias: str) -> None:
    with db.session() as session:
        row = session.get(RegisteredRepo, alias)
        if row is None:
            raise RepoNotFoundError(alias)
        session.exec(_delete_skill_index(alias))
        session.exec(_delete_agent_index(alias))
        session.exec(_delete_rule_index(alias))
        session.delete(row)
        session.commit()
    git.remove_clone(clone_dir(alias))


def _delete_skill_index(alias: str):  # type: ignore[no-untyped-def]
    from sqlmodel import delete as _delete

    return _delete(SkillIndex).where(SkillIndex.repo_alias == alias)  # type: ignore[arg-type]


def _delete_agent_index(alias: str):  # type: ignore[no-untyped-def]
    from sqlmodel import delete as _delete

    from aim.core.models import AgentIndex as _AgentIndex

    return _delete(_AgentIndex).where(_AgentIndex.repo_alias == alias)  # type: ignore[arg-type]


def _delete_rule_index(alias: str):  # type: ignore[no-untyped-def]
    from sqlmodel import delete as _delete

    from aim.core.models import RuleIndex as _RuleIndex

    return _delete(_RuleIndex).where(_RuleIndex.repo_alias == alias)  # type: ignore[arg-type]


def rename(old: str, new: str) -> RegisteredRepo:
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
                    session.commit()
            raise
    return get(new)


class RefDisappearedError(RuntimeError):
    """default_ref no longer resolves on the remote (branch deleted, etc.)."""


def refresh(alias: str, *, allow_insecure: bool = False) -> RegisteredRepo:
    row = get(alias)
    content_guard.require_secure_url(row.url, allow_insecure=allow_insecure)
    previous_sha = row.last_sha
    repo_dir = clone_dir(alias)
    try:
        git.get_backend().fetch(repo_dir)
    except git.GitError as exc:
        raise _wrap_git_error(alias, row.url, exc) from exc

    ref_missing = False
    try:
        new_sha: str | None = git.get_backend().resolve_ref(repo_dir, row.default_ref)
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
            f"{alias}: default ref {row.default_ref!r} no longer resolves upstream"
        )

    if new_sha != previous_sha:
        _skills = importlib.import_module("aim.core.skills")
        _agents = importlib.import_module("aim.core.agents")
        _repo_rules = importlib.import_module("aim.core.repo_rules")

        _skills.index_repo(alias)
        _agents.index_repo(alias)
        _repo_rules.index_repo(alias)
    return fresh
