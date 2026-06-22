"""Helpers shared across CLI command groups.

Kept free of eager `aim.core` imports so importing `aim.cli` stays cheap: the heavy
domain modules are imported inside the functions that use them (and inside the
`_friendly` error handler), not at module top.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from aim.core import format as format_mod

if TYPE_CHECKING:
    from aim.core import risk as risk_mod


def _here(project: Path | None) -> Path:
    """Resolve the project root. Expands `~` so CLI users can pass `~/proj`
    without `init` creating a literal `~/` directory in cwd."""
    if project is None:
        return Path.cwd()
    return project.expanduser()


def _get_format(ctx: typer.Context) -> str:
    """Read the output format selected by the global --format/--json flag."""
    return (ctx.obj or {}).get("format", format_mod.OutputFormat.TABLE)


def _get_allow_insecure(ctx: typer.Context) -> bool:
    """Read the global --allow-insecure flag, defaulting to False."""
    return bool((ctx.obj or {}).get("allow_insecure", False))


def _normalize_repo_url(url: str) -> str:
    """Canonicalize a git URL for equality comparison: drop a trailing `.git`,
    rewrite `git@host:path` to `https://host/path`, and lowercase."""
    u = url.strip()
    if u.startswith("git@") and ":" in u:
        host, _, path = u[len("git@") :].partition(":")
        u = f"https://{host}/{path}"
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/").lower()


def _alias_from_url(url: str) -> str:
    """Derive a repo alias from the last path segment of a git URL."""
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    segment = u.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    alias = re.sub(r"[^a-z0-9_-]", "-", segment.lower()).strip("-")
    return alias or "repo"


# Web "tree"/"blob" URLs (GitHub, GitLab, Gitea, ...) that point at a path
# *inside* a repo on a branch: <scheme>://<host>/<org>/<repo>/tree/<ref>/<subpath>.
# These are not cloneable as-is; we split out the clone URL, ref, and subpath.
_TREE_URL_RE = re.compile(
    r"^(?P<base>https?://[^/]+/[^/]+/[^/]+?)(?:\.git)?/(?:tree|blob|-/tree|-/blob)/"
    r"(?P<ref>[^/]+)/(?P<subpath>.+?)/?$"
)


def _parse_source_url(url: str) -> tuple[str, str | None, str | None]:
    """Split a source URL into (clone_url, ref, inferred_name).

    A plain clone URL passes through unchanged with no ref/name. A web
    tree/blob URL is decomposed so the repo can actually be cloned and the
    artifact name inferred from the in-repo path (a `<name>.md` file yields its
    stem; a `<name>/SKILL.md`/`AGENT.md` yields the directory name; a bare
    directory yields its last segment)."""
    match = _TREE_URL_RE.match(url.strip())
    if match is None:
        return url, None, None
    clone_url = match.group("base")
    ref = match.group("ref")
    segments = [s for s in match.group("subpath").split("/") if s]
    name: str | None = None
    if segments:
        last = segments[-1]
        if last.lower() in ("skill.md", "agent.md"):
            name = segments[-2] if len(segments) >= 2 else None
        elif last.endswith(".md"):
            name = last[:-3]
        else:
            name = last
    return clone_url, ref, name


def _resolve_or_register_repo(
    url: str,
    *,
    alias: str | None,
    allow_insecure: bool,
    default_ref: str | None = None,
    assume_yes: bool = False,
) -> str:
    """Resolve a full git URL to a registered repo alias, registering it if
    necessary. Reuses an existing alias when the URL is already registered;
    otherwise prompts before registering under `alias` (or one derived from the
    URL). Pass `assume_yes` to skip the prompt."""
    from aim.core import repos as repos_mod

    target = _normalize_repo_url(url)
    for repo in repos_mod.list_repos():
        if _normalize_repo_url(repo.url) == target:
            return repo.alias
    chosen = alias or _alias_from_url(url)
    try:
        existing = repos_mod.get(chosen)
    except repos_mod.RepoNotFoundError:
        existing = None
    if existing is not None and _normalize_repo_url(existing.url) != target:
        raise typer.BadParameter(
            f"alias {chosen!r} already maps to {existing.url}; pass --alias to choose another"
        )
    if existing is None:
        if not assume_yes and not typer.confirm(
            f"Repo {chosen!r} ({url}) is not registered. Register it now?", default=True
        ):
            raise typer.Abort()
        typer.echo(f"registering repo {chosen!r} -> {url}")
        repos_mod.add(
            chosen,
            url,
            default_ref=default_ref or "HEAD",
            allow_empty=True,
            allow_insecure=allow_insecure,
        )
    return chosen


def _looks_like_url(target: str) -> bool:
    """Return whether `target` looks like a git URL (scheme or `git@host:`)."""
    t = target.strip()
    return "://" in t or (t.startswith("git@") and ":" in t)


_QUALIFIED_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*$")


def _qualified_for_add(
    ctx: typer.Context,
    target: str,
    name: str | None,
    alias: str | None,
    kind: str,
    *,
    assume_yes: bool = False,
) -> str:
    """Resolve `add`'s positional arg to a qualified name.

    Two forms are accepted:
    - a git URL (clone or web tree/blob) — registers the repo (after a prompt,
      unless `assume_yes`) and infers NAME from the URL path when omitted;
    - a bare `<alias>/<name>` against an already-registered repo — never
      registers; fails if the repo isn't registered yet.
    """
    from aim.core import repos as repos_mod

    if not _looks_like_url(target):
        if name is None and _QUALIFIED_NAME_RE.match(target):
            repo_alias = target.split("/", 1)[0]
            if repo_alias not in {r.alias for r in repos_mod.list_repos()}:
                raise typer.BadParameter(
                    f"repo {repo_alias!r} is not registered. Add it first "
                    f"(`aim repo add {repo_alias} <git-url>`), then retry."
                )
            return target
        raise typer.BadParameter(f"expected a git URL or '<alias>/<name>', got {target!r}")

    clone_url, ref, name_hint = _parse_source_url(target)
    resolved = name or name_hint
    if not resolved:
        raise typer.BadParameter(
            f"could not infer the {kind} name from {target!r}; pass NAME explicitly"
        )
    repo_alias = _resolve_or_register_repo(
        clone_url,
        alias=alias,
        allow_insecure=_get_allow_insecure(ctx),
        default_ref=ref,
        assume_yes=assume_yes,
    )
    return f"{repo_alias}/{resolved}"


def friendly_error_types() -> tuple[type[Exception], ...]:
    """Build the tuple of domain exceptions that map to friendly CLI errors.

    Imports the domain modules lazily (only when an error is actually being classified
    or a command opts in explicitly), so importing `aim.cli` never pulls in the whole
    `aim.core` surface. `FileNotFoundError` is DELIBERATELY excluded — it's too broad
    (a malformed template path would be silenced). List the project-specific subclasses
    explicitly so other I/O errors still produce a real traceback.
    """
    from aim.core import agent_install as agent_install_mod
    from aim.core import agents as agents_mod
    from aim.core import agents_md as agents_md_mod
    from aim.core import archetype_install as archetype_install_mod
    from aim.core import archetypes as archetypes_mod
    from aim.core import content_guard as content_guard_mod
    from aim.core import git
    from aim.core import install as install_mod
    from aim.core import manifest as manifest_mod
    from aim.core import mcp_install as mcp_install_mod
    from aim.core import mcp_registry as mcp_registry_mod
    from aim.core import policy as policy_mod
    from aim.core import profiles as profiles_mod
    from aim.core import prune as prune_mod
    from aim.core import repo_rules as repo_rules_mod
    from aim.core import repos as repos_mod
    from aim.core import risk as risk_mod
    from aim.core import rule_install as rule_install_mod
    from aim.core import skills as skills_mod
    from aim.core import sync as sync_mod
    from aim.core import templates as templates_mod

    return (
        repos_mod.RepoNotFoundError,
        repos_mod.RepoExistsError,
        repos_mod.RepoAliasError,
        repos_mod.RepoHasNoSkillsError,
        repos_mod.RepoHasNoArtifactsError,
        repos_mod.RefDisappearedError,
        rule_install_mod.RuleNotIndexedError,
        rule_install_mod.RuleNotInstalledError,
        rule_install_mod.RuleSourcePathChangedError,
        rule_install_mod.RuleLocalEditsError,
        rule_install_mod.RuleNoHistoryToRollbackError,
        repo_rules_mod.RuleNotIndexedError,
        archetypes_mod.ArchetypeNotIndexedError,
        archetype_install_mod.NoArchetypeSelectedError,
        content_guard_mod.InsecureTransportError,
        content_guard_mod.HiddenUnicodeError,
        policy_mod.PolicyViolationError,
        policy_mod.PolicyError,
        risk_mod.RiskBlockedError,
        install_mod.SkillNotIndexedError,
        skills_mod.SkillNotIndexedError,
        install_mod.SkillNotInstalledError,
        install_mod.SkillSourcePathChangedError,
        install_mod.LocalEditsError,
        install_mod.NoHistoryToRollbackError,
        install_mod.RollbackUnavailableError,
        agent_install_mod.AgentNotIndexedError,
        agents_mod.AgentNotIndexedError,
        agent_install_mod.AgentNotInstalledError,
        agent_install_mod.AgentSourcePathChangedError,
        agent_install_mod.AgentLocalEditsError,
        agent_install_mod.AgentNoHistoryToRollbackError,
        mcp_install_mod.McpAliasInvalidError,
        mcp_install_mod.McpAliasConflictError,
        mcp_install_mod.McpServerNotInstalledError,
        mcp_install_mod.McpLocalEditsError,
        mcp_install_mod.McpNoHistoryToRollbackError,
        mcp_install_mod.McpOverrideError,
        mcp_registry_mod.McpRegistryError,
        mcp_registry_mod.McpMappingError,
        templates_mod.TemplateNotFoundError,
        manifest_mod.ManifestNotFoundError,
        profiles_mod.ProfileNameError,
        profiles_mod.ProfileNotFoundError,
        profiles_mod.ProfileTomlError,
        profiles_mod.TemplateNotLockedError,
        profiles_mod.NoProjectTemplateError,
        profiles_mod.TemplateArtifactNotFoundError,
        sync_mod.SyncError,
        sync_mod.SyncDriftError,
        prune_mod.PruneError,
        git.GitError,
        agents_md_mod.RegionError,
    )


def _render_risk_block(exc: risk_mod.RiskBlockedError) -> None:
    """Render a blocked-risk verdict. Judge findings (which name a rule) go in a
    Rule/Evidence table; screen findings (no rule) are plain lines — never a `—` row."""
    err = Console(stderr=True)
    err.print(
        f"[bold red]risk blocked[/bold red] {exc.source}  [red]{exc.level} ≥ {exc.threshold}[/red]"
    )
    rule_findings = [(r, e) for r, e in exc.violations if r]
    plain = [e for r, e in exc.violations if not r]
    if not exc.violations:
        err.print(f"  {exc}")
    for evidence in plain:
        err.print(f"  [red]•[/red] {evidence}")
    if rule_findings:
        table = Table(
            show_header=True, header_style="bold", box=box.ROUNDED, border_style="red", expand=True
        )
        table.add_column("Rule", style="yellow", no_wrap=True)
        table.add_column("Evidence", style="white", overflow="fold")
        for rule, evidence in rule_findings:
            table.add_row(rule, evidence or "—")
        err.print(table)
    if exc.override_hint:
        err.print(f"[dim]{exc.override_hint}[/dim]")


def _render_error_list(title: str, items: list[str], fallback: str) -> None:
    """Print a titled, one-per-line list of errors, or a single fallback line.

    Args:
        title: Heading shown before the list (e.g. "sync failed").
        items: Individual error strings; each is printed on its own line.
        fallback: Message printed when `items` is empty.
    """
    err = Console(stderr=True)
    if not items:
        err.print(f"[bold red]error:[/bold red] {fallback}")
        return
    err.print(f"[bold red]{title}[/bold red] ({len(items)}):")
    for item in items:
        err.print(f"  [red]•[/red] {item}")


def _friendly(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a command so domain exceptions become friendly CLI errors.

    Args:
        fn: The command function to wrap.

    Returns:
        A wrapper that renders known errors as `error:` messages, exits with
        code 1, and drains any buffered install/risk warnings.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Run the wrapped command, mapping domain errors to CLI exits."""
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            # Control-flow exits Typer must handle — never classify these as domain
            # errors (and don't pay to import the whole core surface to check).
            raise
        except Exception as exc:
            from aim.core import db as db_mod
            from aim.core import risk as risk_mod
            from aim.core import sync as sync_mod

            if isinstance(exc, risk_mod.RiskBlockedError):
                _render_risk_block(exc)
                raise typer.Exit(code=1) from exc
            if isinstance(exc, sync_mod.SyncError):
                _render_error_list("sync failed", exc.errors, str(exc))
                raise typer.Exit(code=1) from exc
            if isinstance(exc, friendly_error_types()):
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            # A leftover "database is locked" (e.g. another aim/TUI process holds it)
            # gets a clean, actionable message instead of a raw SQLAlchemy traceback.
            if not db_mod.is_locked_error(exc):
                raise
            Console(stderr=True).print(
                "[bold red]error:[/bold red] the aim database is locked — another aim "
                "or TUI process may be running.\n"
                "  close it and retry, or run [bold]aim db unlock[/bold] to recover."
            )
            raise typer.Exit(code=1) from exc
        finally:
            # Surface buffered warnings from any deploy path (add/update/rollback/
            # sync) so they are never silently dropped — even when the command
            # raised before its own inline drain. Buffers are emptied by the drain,
            # so commands that already drained inline print nothing twice.
            from aim.core import agent_install as agent_install_mod
            from aim.core import install as install_mod
            from aim.core import risk as risk_mod

            for warn in install_mod.take_install_warnings():
                typer.echo(f"  warning: {warn}", err=True)
            for warn in agent_install_mod.take_install_warnings():
                typer.echo(f"  warning: {warn}", err=True)
            for warn in risk_mod.take_risk_warnings():
                typer.echo(f"  risk: {warn}", err=True)

    return wrapper


def _scanning(label: str) -> Any:
    """A transient stderr spinner for the duration of an add. The risk scan can pull a
    model or call a judge, so signal that work is happening instead of hanging silently."""
    return Console(stderr=True).status(label, spinner="dots")


def _run_bulk_update(
    update_many: Callable[..., list[dict[str, Any]]],
    project: Path | None,
    repo: str | None,
    only_outdated: bool,
    force: bool,
    override_risk: bool = False,
) -> None:
    """Run a kind's bulk update_many, print per-artifact outcomes, exit 1 on any error.

    Args:
        update_many: The core `update_many` for the artifact kind.
        project: Project root (defaults to cwd).
        repo: Limit to a single repo alias, or None for all.
        only_outdated: Skip artifacts already at HEAD.
        force: Overwrite local edits.
        override_risk: Bypass a risk gate the user has acknowledged.
    """
    outcomes = update_many(
        _here(project),
        repo_alias=repo,
        only_outdated=only_outdated,
        force=force,
        override_risk=override_risk,
    )
    for outcome in outcomes:
        typer.echo(f"{outcome['status']:>12}  {outcome['qualified_name']}  {outcome['detail']}")
    if any(outcome["status"] == "error" for outcome in outcomes):
        raise typer.Exit(code=1)
