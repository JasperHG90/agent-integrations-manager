"""Typer entry point. CLI is the surface for scripting/CI; the TUI uses the
same core API.
"""

from __future__ import annotations

import asyncio
import functools
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from aim import __version__
from aim.core import agent_install as agent_install_mod
from aim.core import agents as agents_mod
from aim.core import agents_md as agents_md_mod
from aim.core import content_guard as content_guard_mod
from aim.core import doctor as doctor_mod
from aim.core import format as format_mod
from aim.core import git, hashing
from aim.core import init as init_mod
from aim.core import install as install_mod
from aim.core import lock as lock_mod
from aim.core import mcp_install as mcp_install_mod
from aim.core import mcp_registry as mcp_registry_mod
from aim.core import profiles as profiles_mod
from aim.core import prune as prune_mod
from aim.core import repo_rules as repo_rules_mod
from aim.core import repos as repos_mod
from aim.core import roots as roots_mod
from aim.core import rule_install as rule_install_mod
from aim.core import skills as skills_mod
from aim.core import sync as sync_mod
from aim.core import templates as templates_mod

app = typer.Typer(
    add_completion=False,
    help="Scaffold agent-engineering projects. Run with no arguments to launch the TUI.",
    invoke_without_command=True,
)
rule_app = typer.Typer(no_args_is_help=True, help="Discover and manage repo-sourced rules.")
repo_app = typer.Typer(no_args_is_help=True, help="Manage skill/agent/rule source repositories.")
skill_app = typer.Typer(no_args_is_help=True, help="Discover and manage skills.")
subagent_app = typer.Typer(no_args_is_help=True, help="Discover and manage sub-agents.")
app.add_typer(rule_app, name="rule")
app.add_typer(repo_app, name="repo")
app.add_typer(skill_app, name="skill")
app.add_typer(subagent_app, name="subagent")


from aim.core import manifest as manifest_mod  # noqa: E402

# Map domain exceptions to friendly CLI errors. Note: `FileNotFoundError` is
# DELIBERATELY excluded — it's too broad (a malformed template path would be
# silenced). List the project-specific subclasses explicitly so other I/O
# errors still produce a real traceback.
_FRIENDLY_ERRORS: tuple[type[Exception], ...] = (
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
    content_guard_mod.InsecureTransportError,
    content_guard_mod.HiddenUnicodeError,
    install_mod.SkillNotIndexedError,
    install_mod.SkillNotInstalledError,
    install_mod.SkillSourcePathChangedError,
    install_mod.LocalEditsError,
    install_mod.NoHistoryToRollbackError,
    install_mod.RollbackUnavailableError,
    agent_install_mod.AgentNotIndexedError,
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
    sync_mod.SyncError,
    sync_mod.SyncDriftError,
    prune_mod.PruneError,
    git.GitError,
    agents_md_mod.RegionError,
)


def _friendly(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _FRIENDLY_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    return wrapper


def _here(project: Path | None) -> Path:
    """Resolve the project root. Expands `~` so CLI users can pass `~/proj`
    without `init` creating a literal `~/` directory in cwd."""
    if project is None:
        return Path.cwd()
    return project.expanduser()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"aim {__version__}")
        raise typer.Exit()


def _format_callback(ctx: typer.Context, value: str) -> str:
    """Store the selected output format in ctx.obj for subcommands."""
    ctx.obj = ctx.obj or {}
    ctx.obj["format"] = value
    return value


def _allow_insecure_callback(ctx: typer.Context, value: bool) -> bool:
    """Store the global --allow-insecure flag in ctx.obj for subcommands."""
    ctx.obj = ctx.obj or {}
    ctx.obj["allow_insecure"] = value
    return value


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


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root for the TUI (default: current directory).",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Layout profile to use when launching the TUI.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output list commands as JSON instead of tables.",
    ),
    output_format: str = typer.Option(
        format_mod.OutputFormat.TABLE,
        "--format",
        help="Output format for list commands: table, json, or compact.",
        callback=_format_callback,
        is_eager=True,
    ),
    compact_output: bool = typer.Option(
        False,
        "--compact",
        help="Output list commands as compact NDJSON (one JSON object per line).",
    ),
    allow_insecure: bool = typer.Option(
        False,
        "--allow-insecure",
        help="Allow plain http:// transports for repos and MCP servers.",
        callback=_allow_insecure_callback,
        is_eager=True,
    ),
) -> None:
    """aim: scaffold and manage agent-engineering projects.

    With no subcommand, launches the Textual TUI — the primary surface.
    Subcommands are available for scripting/CI.
    """
    if json_output:
        ctx.obj = ctx.obj or {}
        ctx.obj["format"] = format_mod.OutputFormat.JSON
    if compact_output:
        ctx.obj = ctx.obj or {}
        ctx.obj["format"] = format_mod.OutputFormat.COMPACT
    if ctx.invoked_subcommand is None:
        import sys

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            typer.echo(ctx.get_help())
            raise typer.Exit(code=2)
        from aim.tui.app import run as run_tui

        run_tui(project_root=project, profile_name=profile)
        raise typer.Exit()


# ---------- init ----------


@app.command("check")
@_friendly
def check_cmd(
    project: list[Path] = typer.Option(
        [],
        "--project",
        "-p",
        help="Project root to check (repeatable). Defaults to cwd.",
    ),
) -> None:
    """Pre-commit-friendly drift check. Exits non-zero if anything has drifted.

    Faster than `doctor` because it skips the global-cache audits — only
    checks per-project drift (regions + skill content hashes).
    """
    proj_list = [p.expanduser() for p in project] if project else [Path.cwd()]
    bad = 0
    for proj in proj_list:
        try:
            m = manifest_mod.load(proj)
        except manifest_mod.ManifestNotFoundError:
            typer.echo(f"{proj}: no manifest (skipped)", err=True)
            continue
        for managed in m.managed_files:
            target = proj / managed
            if not target.exists():
                typer.echo(f"{proj}/{managed}: missing", err=True)
                bad += 1
                continue
            try:
                regions = agents_md_mod.parse(target.read_text())
            except agents_md_mod.RegionError as exc:
                typer.echo(f"{proj}/{managed}: malformed markers — {exc}", err=True)
                bad += 1
                continue
            for region in regions:
                prior = m.managed_region_hashes.get(region.name)
                if prior is None:
                    continue
                if hashing.hash_text(region.body) != prior:
                    typer.echo(
                        f"{proj}/{managed}: region {region.name!r} drifted",
                        err=True,
                    )
                    bad += 1
        for skill in m.skills:
            target = proj / skill.target_dir
            if skill.content_hash is None or not target.exists():
                continue
            if hashing.hash_tree(target) != skill.content_hash:
                typer.echo(
                    f"{proj}/{skill.target_dir}: skill {skill.qualified_name} drifted",
                    err=True,
                )
                bad += 1
        for agent in m.agents:
            target = proj / agent.target_path
            if agent.content_hash is None or not target.exists():
                continue
            if hashing.hash_text(target.read_text(encoding="utf-8")) != agent.content_hash:
                typer.echo(
                    f"{proj}/{agent.target_path}: agent {agent.qualified_name} drifted",
                    err=True,
                )
                bad += 1
        try:
            mcp_data = mcp_registry_mod.read_mcp_json(proj)
        except mcp_registry_mod.McpRegistryError as exc:
            typer.echo(f"{proj}/.mcp.json: invalid — {exc}", err=True)
            bad += 1
            mcp_data = {"mcpServers": {}}
        mcp_servers = mcp_data.get("mcpServers", {})
        for mcp in m.mcp_servers:
            if not isinstance(mcp_servers, dict) or mcp.alias not in mcp_servers:
                typer.echo(f"{proj}/.mcp.json: MCP alias {mcp.alias!r} missing", err=True)
                bad += 1
                continue
            current_hash = hashing.hash_text(
                mcp_registry_mod._canonical_json(mcp_servers[mcp.alias])
            )
            if current_hash != mcp.entry_hash:
                typer.echo(
                    f"{proj}/.mcp.json: MCP alias {mcp.alias!r} drifted",
                    err=True,
                )
                bad += 1
    if bad:
        raise typer.Exit(code=1)
    typer.echo("clean")


@app.command("doctor")
@_friendly
def doctor_cmd(
    project: list[Path] = typer.Option(
        [],
        "--project",
        "-p",
        help="Project root to audit (repeatable). Defaults to roots configured in user config.",
    ),
    stale_days: int = typer.Option(
        30, "--stale-days", help="Repos not fetched in this many days flag a warning."
    ),
) -> None:
    """Audit drift across projects + global cache health."""
    project_roots = [p.expanduser() for p in project] if project else None
    report = doctor_mod.audit(project_roots=project_roots, stale_repo_days=stale_days)
    typer.echo(f"audited {report.projects_audited} project(s)")
    counts = {sev: len(report.by_severity(sev)) for sev in ("error", "warning", "info")}
    typer.echo(
        f"  errors:   {counts['error']}\n"
        f"  warnings: {counts['warning']}\n"
        f"  info:     {counts['info']}"
    )
    for finding in report.findings:
        prefix = f"[{finding.severity}] " + (f"{finding.project}: " if finding.project else "")
        typer.echo(prefix + finding.message)
    if not report.ok:
        raise typer.Exit(code=1)


root_app = typer.Typer(
    no_args_is_help=True, help="Manage the global list of project roots used by `doctor`."
)
app.add_typer(root_app, name="root")


@root_app.command("add")
@_friendly
def root_add(path: Path = typer.Argument(..., help="Project root path.")) -> None:
    """Track a project root so `aim doctor` and global commands can find it."""
    roots_mod.add_root(path.expanduser())
    typer.echo(f"added root {path.expanduser().resolve()}")


@root_app.command("list")
@_friendly
def root_list(ctx: typer.Context) -> None:
    """List configured project roots."""
    entries = roots_mod.list_roots()
    rows = [{"path": str(r)} for r in entries]
    format_mod.render(
        rows,
        _get_format(ctx),
        title="roots configured",
        columns=["path"],
        compact_columns=["path"],
    )


@root_app.command("remove")
@_friendly
def root_remove(path: Path = typer.Argument(..., help="Project root path.")) -> None:
    """Stop tracking a project root."""
    removed = roots_mod.remove_root(path.expanduser())
    typer.echo(f"removed {path}" if removed else f"not in roots: {path}")


profile_app = typer.Typer(
    no_args_is_help=True,
    help="Manage and apply reusable project templates (profiles).",
)
app.add_typer(profile_app, name="profile")


@profile_app.command("save")
@_friendly
def profile_save(
    name: str,
    project: Path | None = typer.Argument(None, help="Project to snapshot as a reusable template."),
) -> None:
    """Snapshot a project's declarations into a reusable named profile."""
    profile = profiles_mod.from_project(name, _here(project))
    path = profiles_mod.save(profile)
    typer.echo(f"saved project template {name} to {path}")


@profile_app.command("list")
@_friendly
def profile_list(ctx: typer.Context) -> None:
    """List saved project profiles."""
    entries = profiles_mod.list_profiles()
    rows = [
        {
            "name": p.name,
            "instruction_template": p.instruction_template,
            "symlinks": ",".join(p.symlinks) or "-",
            "skills": len(p.skills),
            "subagents": len(p.agents),
            "mcp": len(p.mcp_servers),
            "rules": len(p.rules),
        }
        for p in entries
    ]
    format_mod.render(
        rows,
        _get_format(ctx),
        title="profiles saved",
        columns=["name", "instruction_template", "symlinks", "skills", "subagents", "mcp", "rules"],
        compact_columns=["name", "instruction_template", "skills", "subagents", "mcp", "rules"],
    )


@profile_app.command("show")
@_friendly
def profile_show(name: str) -> None:
    """Print a saved profile as JSON."""
    p = profiles_mod.load(name)
    typer.echo(p.model_dump_json(indent=2))


@profile_app.command("delete")
@_friendly
def profile_delete(name: str) -> None:
    """Delete a saved profile."""
    removed = profiles_mod.delete(name)
    typer.echo(f"deleted {name}" if removed else f"not found: {name}")


@profile_app.command("apply")
@_friendly
def profile_apply(
    ctx: typer.Context,
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Apply a saved profile to a project: init, lock, install artifacts, sync."""
    result = profiles_mod.apply(name, _here(project), allow_insecure=_get_allow_insecure(ctx))
    typer.echo(f"applied project template {name} to {result.project_root}")
    for qn in result.installed_skills:
        typer.echo(f"  installed skill: {qn}")
    for qn in result.skipped_skills:
        typer.echo(f"  skipped skill (not indexed locally): {qn}", err=True)
    for qn in result.installed_agents:
        typer.echo(f"  installed agent: {qn}")
    for qn in result.skipped_agents:
        typer.echo(f"  skipped agent (not indexed locally): {qn}", err=True)
    for alias in result.installed_mcp:
        typer.echo(f"  installed MCP server: {alias}")
    for alias in result.skipped_mcp:
        typer.echo(f"  skipped MCP server (unavailable): {alias}", err=True)


mcp_app = typer.Typer(
    no_args_is_help=True,
    help="MCP servers: manage .mcp.json entries via the public registry.",
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("search")
@_friendly
def mcp_search_cmd(query: str = typer.Argument(..., help="Search term.")) -> None:
    """Search the public MCP registry."""
    results, _ = mcp_registry_mod.search_registry(query)
    if not results:
        typer.echo(f"no MCP servers match {query!r}")
        return
    for r in results:
        server = r.server
        desc = f" — {server.description}" if server.description else ""
        typer.echo(f"{server.name}{desc}")


@mcp_app.command("list")
@_friendly
def mcp_list_cmd(
    ctx: typer.Context,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """List MCP servers installed in the project."""
    m = manifest_mod.load_or_default(_here(project))
    format_mod.render(
        m.mcp_servers,
        _get_format(ctx),
        title="MCP servers installed",
        columns=["alias", "registry_name", "version"],
        row_extractor={
            "alias": "alias",
            "registry_name": "registry_name",
            "version": "current.registry_version",
        },
        compact_columns=["alias", "registry_name", "version"],
    )


@mcp_app.command("add")
@mcp_app.command("install", hidden=True)
@_friendly
def mcp_add_cmd(
    ctx: typer.Context,
    registry_name: str = typer.Argument(..., help="Canonical registry server name."),
    alias: str = typer.Argument(..., help="Local alias for .mcp.json -> mcpServers."),
    project: Path | None = typer.Argument(None, help="Project root."),
    transport: str | None = typer.Option(
        None, "--transport", help="Preferred transport: stdio, http, sse, ws."
    ),
    command: str | None = typer.Option(None, "--command", help="Override entry command."),
    arg: list[str] = typer.Option([], "--arg", help="Override entry args (repeatable)."),
    env: list[str] = typer.Option([], "--env", help="Override env var NAME=VALUE (repeatable)."),
    url: str | None = typer.Option(None, "--url", help="Override entry URL."),
    header: list[str] = typer.Option(
        [], "--header", help="Override header Name:Value (repeatable)."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing alias."),
) -> None:
    """Add an MCP server to the project's .mcp.json (by registry name)."""
    overrides: dict[str, object] = {}
    if command:
        overrides["command"] = command
    if arg:
        overrides["args"] = list(arg)
    if env:
        overrides["env"] = _parse_key_value_list(env)
    if url:
        overrides["url"] = url
    if header:
        overrides["headers"] = _parse_header_list(header)
    installed = mcp_install_mod.install(
        _here(project),
        registry_name,
        alias=alias,
        preferred_transport=transport,
        overrides=overrides or None,
        force=force,
        allow_insecure=_get_allow_insecure(ctx),
    )
    typer.echo(f"added MCP server {installed.registry_name} as {installed.alias}")


@mcp_app.command("update")
@_friendly
def mcp_update_cmd(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Refresh a managed MCP server from the registry."""
    updated = mcp_install_mod.update(
        _here(project), alias, force=force, allow_insecure=_get_allow_insecure(ctx)
    )
    typer.echo(f"updated MCP server {updated.alias} -> {updated.current.registry_version or '?'}")


@mcp_app.command("remove")
@mcp_app.command("uninstall", hidden=True)
@mcp_app.command("delete", hidden=True)
@_friendly
def mcp_remove_cmd(
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Remove a managed MCP server from .mcp.json."""
    mcp_install_mod.delete(_here(project), alias)
    typer.echo(f"removed MCP server {alias}")


@app.command("tui")
def tui_cmd(
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root (default: current directory).",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Layout profile to use when launching the TUI.",
    ),
) -> None:
    """Launch the Textual TUI."""
    from aim.tui.app import run as run_tui

    run_tui(project_root=project, profile_name=profile)


@app.command("init")
@_friendly
def init_cmd(
    project: Path | None = typer.Argument(None, help="Project root (default: current directory)."),
    instruction_template: str = typer.Option(
        templates_mod.BUILTIN_DEFAULT, "--template", "-t", help="Instruction template name."
    ),
    symlink: list[str] = typer.Option(
        [],
        "--symlink",
        help="Symlink to create pointing at AGENTS.md (repeatable, e.g. CLAUDE.md).",
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", help="Layout profile to use (overrides manifest)."
    ),
) -> None:
    """Create or update the user-editable aim.toml declarations file.

    Rules are repo-sourced; add them after init with `aim rule add <git-url> <name>`.
    """
    options = init_mod.InitOptions(
        project_root=_here(project),
        instruction_template=instruction_template,
        symlinks=tuple(symlink),
        layout_profile=layout_profile,
    )
    result = init_mod.run(options)
    verb = "Refreshed" if result.re_init else "Initialized"
    typer.echo(f"{verb} {result.declarations_path}")
    if result.applied_rules:
        typer.echo(f"  rules:  {', '.join(result.applied_rules)}")
    typer.echo(
        "Run `aim lock` to resolve declarations into aim.lock.toml, then `aim sync` to apply them."
    )


@app.command("lock")
@_friendly
def lock_cmd(
    ctx: typer.Context,
    project: Path | None = typer.Argument(None, help="Project root (default: current directory)."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Always rewrite aim.lock.toml, even if unchanged."
    ),
) -> None:
    """Resolve aim.toml declarations into an exact aim.lock.toml."""
    console = Console()

    async def _run(status: Any) -> lock_mod.LockResult:
        def _progress(kind: str, name: str, state: str) -> None:
            status.update(f"{kind} {name}: {state}")

        return await lock_mod.run(
            lock_mod.LockOptions(
                project_root=_here(project),
                allow_insecure=_get_allow_insecure(ctx),
                progress_callback=_progress,
                force=force,
            )
        )

    with console.status("Locking dependencies...", spinner="dots") as status:
        result = asyncio.run(_run(status))

    if result.unchanged:
        typer.echo("aim.lock.toml up to date; no changes")
        return

    for qn in result.locked_skills:
        typer.echo(f"locked skill {qn}")
    for qn in result.locked_agents:
        typer.echo(f"locked agent {qn}")
    for alias in result.locked_mcp:
        typer.echo(f"locked mcp {alias}")
    for warn in result.warnings:
        typer.echo(f"warning: {warn}", err=True)


@app.command("sync")
@_friendly
def sync_cmd(
    ctx: typer.Context,
    project: Path | None = typer.Argument(None, help="Project root (default: current directory)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    no_sync_agents: bool = typer.Option(
        False, "--no-sync-agents", help="Skip re-rendering AGENTS.md / symlinks."
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", help="Layout profile override (overrides manifest)."
    ),
) -> None:
    """Reproduce the committed project state from aim.lock.toml."""

    async def _run() -> sync_mod.SyncResult:
        return await sync_mod.run(
            sync_mod.SyncOptions(
                project_root=_here(project),
                force=force,
                sync_agents=not no_sync_agents,
                layout_profile=layout_profile,
                allow_insecure=_get_allow_insecure(ctx),
            )
        )

    result = asyncio.run(_run())
    for qn in result.synced_skills:
        typer.echo(f"synced skill {qn}")
    for qn in result.synced_agents:
        typer.echo(f"synced agent {qn}")
    for alias in result.synced_mcp:
        typer.echo(f"synced mcp {alias}")
    for warn in result.drift_warnings:
        typer.echo(f"warning: {warn}", err=True)


@app.command("prune")
@_friendly
def prune_cmd(
    project: Path | None = typer.Argument(None, help="Project root (default: current directory)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show plan and exit (no prompt, no changes)."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "--skip-plan",
        help="Skip the plan prompt and prune immediately.",
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", help="Layout profile to use (overrides manifest)."
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="Glob pattern to protect from pruning (can be repeated)."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show kept items in addition to removals."
    ),
) -> None:
    """Remove lockfile entries no longer declared in aim.toml.

    Prune compares aim.toml (declarations) against aim.lock.toml (installed
    state) and removes entries — and their on-disk files — that are no longer
    declared. Files on disk not tracked by aim (e.g. installed by Terraform or
    other plugins) are left alone.

    By default, prune shows a plan and prompts for confirmation. Use --force
    (or --skip-plan) to apply immediately, or --dry-run to preview only.

    Persistent exclusions can be stored in an .aimignore file at the project
    root with one glob pattern per line, e.g. .claude/skills/local/*.
    """
    import sys

    console = Console()
    err_console = Console(stderr=True)
    options = prune_mod.PruneOptions(
        project_root=_here(project),
        dry_run=dry_run,
        force=force,
        layout_profile=layout_profile,
        excludes=list(exclude),
    )

    try:
        plan_result = prune_mod.plan(options)
    except prune_mod.PruneError as exc:
        err_console.print(f"[bold red]error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    # Missing aim.toml — warnings explain; nothing to apply.
    if not any(i.action == "would-remove" for i in plan_result.removed):
        prune_mod.render_prune_plan(plan_result, verbose=verbose)
        for warning in plan_result.warnings:
            console.print(f"[yellow]warning:[/yellow] {warning}")
        return

    prune_mod.render_prune_plan(plan_result, verbose=verbose)
    for warning in plan_result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    if dry_run:
        return

    if force:
        try:
            apply_result = prune_mod.apply(options, plan_result)
        except prune_mod.PruneError as exc:
            err_console.print(f"[bold red]error:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc
        _print_prune_apply_result(console, apply_result)
        return

    if not sys.stdin.isatty():
        err_console.print(
            "[yellow]warning:[/yellow] not a TTY; use --force to apply. No changes made."
        )
        raise typer.Exit(code=0)

    n = sum(1 for i in plan_result.removed if i.action == "would-remove")
    confirmed = typer.confirm(f"Apply {n} change(s)?", default=False)
    if not confirmed:
        console.print("Aborted. No changes made.")
        return

    try:
        apply_result = prune_mod.apply(options, plan_result)
    except prune_mod.PruneError as exc:
        err_console.print(f"[bold red]error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _print_prune_apply_result(console, apply_result)


def _print_prune_apply_result(console: Console, result: prune_mod.PruneResult) -> None:
    for item in result.removed:
        if item.action == "removed":
            console.print(f"[red]removed[/red] {item.kind} {item.path}")
        elif item.action == "removed-stale-entry":
            console.print(f"[red]removed[/red] (stale) {item.kind} {item.path}")
    for item in result.kept:
        if item.action.startswith("error") or item.action == "skipped-unsafe":
            console.print(f"[yellow]{item.action}[/yellow] {item.kind} {item.path}")
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")


def _print_diffs(changes: list) -> None:  # type: ignore[type-arg]
    import difflib

    for change in changes:
        before = change.before or ""
        before_label = f"a/{change.path}" if change.before is not None else "/dev/null"
        after_label = f"b/{change.path}"
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            change.after.splitlines(keepends=True),
            fromfile=before_label,
            tofile=after_label,
        )
        for line in diff:
            typer.echo(line, nl=False)
        typer.echo("")


# ---------- rule ----------


@rule_app.command("list")
@_friendly
def rule_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    """List indexed rules."""
    rows = repo_rules_mod.list_rules(repo)
    format_mod.render(
        rows,
        _get_format(ctx),
        title="rules indexed",
        columns=["qualified_name", "repo_alias", "title", "description"],
        compact_columns=["qualified_name", "title", "description"],
    )


@rule_app.command("search")
@_friendly
def rule_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Substring to match."),
) -> None:
    """Search indexed rules by substring."""
    rows = repo_rules_mod.search(query)
    format_mod.render(
        rows,
        _get_format(ctx),
        title=f"rules matching {query!r}",
        columns=["qualified_name", "repo_alias", "title", "description"],
        compact_columns=["qualified_name", "title", "description"],
    )


@rule_app.command("add")
@_friendly
def rule_add(
    ctx: typer.Context,
    url: str = typer.Argument(
        ...,
        help="Git URL (clone or web tree/blob), or '<alias>/<name>' of an already-registered repo.",
    ),
    name: str | None = typer.Argument(
        None, help="Rule name within the repo (inferred from a tree/blob URL if omitted)."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    alias: str | None = typer.Option(
        None, "--alias", help="Repo alias to register under (default: derived from the URL)."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None,
        "--track",
        help="Ref to track on update: 'latest-tag', a branch name, or any ref. Overrides repo default_ref.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Register the source repo without prompting."
    ),
) -> None:
    """Add a rule from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "rule", assume_yes=yes)
    installed = rule_install_mod.install(_here(project), qualified_name, pin=pin, track=track)
    typer.echo(f"added rule {qualified_name} {installed.current.identifier()}")


@rule_app.command("update")
@_friendly
def rule_update(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<rule_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Refresh an installed rule from its source repo."""
    updated = rule_install_mod.update(_here(project), qualified_name, force=force)
    typer.echo(f"updated rule {qualified_name} -> {updated.current.identifier()}")


@rule_app.command("update-many")
@_friendly
def rule_update_many(
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    all_rules: bool = typer.Option(False, "--all", help="Update every installed rule."),
    repo: str | None = typer.Option(None, "--repo", help="Limit to a single repo alias."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip rules already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Update installed rules in bulk."""
    if not all_rules and repo is None:
        raise typer.BadParameter("pass --all or --repo <alias>")
    outcomes = rule_install_mod.update_many(
        _here(project),
        repo_alias=repo,
        only_outdated=only_outdated,
        force=force,
    )
    for o in outcomes:
        typer.echo(f"{o['status']:>12}  {o['qualified_name']}  {o['detail']}")
    errors = [o for o in outcomes if o["status"] == "error"]
    if errors:
        raise typer.Exit(code=1)


@rule_app.command("remove")
@_friendly
def rule_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<rule_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed rule from the project."""
    rule_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed rule {qualified_name}")


@rule_app.command("rollback")
@_friendly
def rule_rollback(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<rule_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a rule."""
    rolled = rule_install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back rule {qualified_name} -> {rolled.current.identifier()}")


# ---------- repo ----------


@repo_app.command("add")
@_friendly
def repo_add(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Local alias for the source repo."),
    url: str = typer.Argument(..., help="Git URL (https or ssh or file:// for local)."),
    default_ref: str = typer.Option(
        "HEAD", "--ref", help="Default ref to resolve on refresh (branch or tag)."
    ),
    allow_empty: bool = typer.Option(
        False, "--allow-empty", help="Allow registering a repo with no discoverable skills."
    ),
) -> None:
    """Register and bare-clone a skill source repository."""
    repo = repos_mod.add(
        alias,
        url,
        default_ref=default_ref,
        allow_empty=allow_empty,
        allow_insecure=_get_allow_insecure(ctx),
    )
    typer.echo(f"added repo {repo.alias} -> {repo.url}")
    if repo.last_sha:
        typer.echo(f"  HEAD: {repo.last_sha[:12]}")


@repo_app.command("list")
@_friendly
def repo_list(ctx: typer.Context) -> None:
    """List registered skill source repositories."""
    repos = repos_mod.list_repos()
    format_mod.render(
        repos,
        _get_format(ctx),
        title="repos registered",
        columns=["alias", "url", "default_ref", "head", "last_fetched"],
        row_extractor={
            "alias": "alias",
            "url": "url",
            "default_ref": "default_ref",
            "head": "last_sha",
            "last_fetched": "last_fetched_at",
        },
        compact_columns=["alias", "url", "default_ref"],
    )


@repo_app.command("remove")
@_friendly
def repo_remove(alias: str) -> None:
    """Unregister a source repo and delete its local clone."""
    repos_mod.remove(alias)
    typer.echo(f"removed repo {alias}")


@repo_app.command("rename")
@_friendly
def repo_rename(old: str, new: str) -> None:
    """Rename a registered repo alias (moves its clone and index rows)."""
    repos_mod.rename(old, new)
    typer.echo(f"renamed {old} -> {new}")


@repo_app.command("refresh")
@_friendly
def repo_refresh(
    ctx: typer.Context,
    alias: str,
) -> None:
    """Fetch the latest commits for a registered repo and re-index its artifacts."""
    repo = repos_mod.refresh(alias, allow_insecure=_get_allow_insecure(ctx))
    sha = repo.last_sha[:12] if repo.last_sha else "?"
    typer.echo(f"refreshed {alias}: HEAD={sha}")


# ---------- skill ----------


@skill_app.command("list")
@_friendly
def skill_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    """List indexed skills."""
    rows = skills_mod.list_skills(repo)
    format_mod.render(
        rows,
        _get_format(ctx),
        title="skills indexed",
        columns=["qualified_name", "repo_alias", "title", "description"],
        compact_columns=["qualified_name", "title", "description"],
    )


@skill_app.command("search")
@_friendly
def skill_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Substring to match."),
) -> None:
    """Search indexed skills by substring."""
    rows = skills_mod.search(query)
    format_mod.render(
        rows,
        _get_format(ctx),
        title=f"skills matching {query!r}",
        columns=["qualified_name", "repo_alias", "title", "description"],
        compact_columns=["qualified_name", "title", "description"],
    )


@skill_app.command("add")
@_friendly
def skill_add(
    ctx: typer.Context,
    url: str = typer.Argument(
        ...,
        help="Git URL (clone or web tree/blob), or '<alias>/<name>' of an already-registered repo.",
    ),
    name: str | None = typer.Argument(
        None, help="Skill name within the repo (inferred from a tree/blob URL if omitted)."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    alias: str | None = typer.Option(
        None, "--alias", help="Repo alias to register under (default: derived from the URL)."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None,
        "--track",
        help="Ref to track on update: 'latest-tag', a branch name, or any ref. Overrides repo default_ref.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Register the source repo without prompting."
    ),
) -> None:
    """Add a skill from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "skill", assume_yes=yes)
    installed = install_mod.install(_here(project), qualified_name, pin=pin, track=track)
    typer.echo(f"added {qualified_name} {installed.current.identifier()} -> {installed.target_dir}")
    for warn in install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)


@skill_app.command("install", hidden=True)
@_friendly
def skill_install(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<skill_name>"),
    project: Path | None = typer.Argument(None, help="Project root."),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None,
        "--track",
        help="Ref to track on update: 'latest-tag', a branch name, or any ref. Overrides repo default_ref.",
    ),
) -> None:
    """Deprecated: install an already-registered skill by qualified name. Use `add`."""
    installed = install_mod.install(_here(project), qualified_name, pin=pin, track=track)
    typer.echo(
        f"installed {qualified_name} {installed.current.identifier()} -> {installed.target_dir}"
    )
    for warn in install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)


@skill_app.command("update")
@_friendly
def skill_update(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    diff: bool = typer.Option(False, "--diff", help="Show proposed version change; don't apply."),
) -> None:
    """Refresh an installed skill from its source repo."""
    if diff:
        preview = install_mod.update(_here(project), qualified_name, dry_run=True)
        assert isinstance(preview, install_mod.UpdatePreview)
        verb = "WOULD UPDATE" if preview.will_change else "no-op"
        ident = (
            f"{preview.proposed_tag}+{preview.proposed_sha[:7]}"
            if preview.proposed_tag
            else preview.proposed_sha[:7]
        )
        typer.echo(f"{verb} {qualified_name}: {preview.current_sha[:7]} -> {ident}")
        return
    updated = install_mod.update(_here(project), qualified_name, force=force)
    assert not isinstance(updated, install_mod.UpdatePreview)
    typer.echo(f"updated {qualified_name} -> {updated.current.identifier()}")


@skill_app.command("update-many")
@_friendly
def skill_update_many(
    project: Path | None = typer.Argument(None),
    all_skills: bool = typer.Option(False, "--all", help="Update every installed skill."),
    repo: str | None = typer.Option(None, "--repo", help="Limit to a single repo alias."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip skills already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    diff: bool = typer.Option(False, "--diff", help="Show proposals; don't apply."),
) -> None:
    """Update installed skills in bulk."""
    if not all_skills and repo is None:
        raise typer.BadParameter("pass --all or --repo <alias>")
    outcomes = install_mod.update_many(
        _here(project),
        repo_alias=repo,
        only_outdated=only_outdated,
        force=force,
        dry_run=diff,
    )
    for o in outcomes:
        typer.echo(f"{o.status:>12}  {o.qualified_name}  {o.detail}")
    errors = [o for o in outcomes if o.status == "error"]
    if errors:
        raise typer.Exit(code=1)


@skill_app.command("remove")
@_friendly
def skill_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<skill_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed skill from the project."""
    install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed {qualified_name}")


@skill_app.command("uninstall", hidden=True)
@_friendly
def skill_uninstall(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    skill_remove(qualified_name, project)


@skill_app.command("delete", hidden=True)
@_friendly
def skill_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    skill_remove(qualified_name, project)


@skill_app.command("rollback")
@_friendly
def skill_rollback(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a skill."""
    rolled = install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back {qualified_name} -> {rolled.current.identifier()}")


# ---------- agent ----------


@subagent_app.command("list")
@_friendly
def agent_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    """List indexed sub-agents."""
    rows = agents_mod.list_agents(repo)
    format_mod.render(
        rows,
        _get_format(ctx),
        title="subagents indexed",
        columns=["qualified_name", "repo_alias", "title", "description"],
        compact_columns=["qualified_name", "title", "description"],
    )


@subagent_app.command("search")
@_friendly
def agent_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Substring to match."),
) -> None:
    """Search indexed sub-agents by substring."""
    rows = agents_mod.search(query)
    format_mod.render(
        rows,
        _get_format(ctx),
        title=f"subagents matching {query!r}",
        columns=["qualified_name", "repo_alias", "title", "description"],
        compact_columns=["qualified_name", "title", "description"],
    )


@subagent_app.command("add")
@_friendly
def agent_add(
    ctx: typer.Context,
    url: str = typer.Argument(
        ...,
        help="Git URL (clone or web tree/blob), or '<alias>/<name>' of an already-registered repo.",
    ),
    name: str | None = typer.Argument(
        None, help="Sub-agent name within the repo (inferred from a tree/blob URL if omitted)."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    alias: str | None = typer.Option(
        None, "--alias", help="Repo alias to register under (default: derived from the URL)."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None,
        "--track",
        help="Ref to track on update: 'latest-tag', a branch name, or any ref. Overrides repo default_ref.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Register the source repo without prompting."
    ),
) -> None:
    """Add a sub-agent from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "sub-agent", assume_yes=yes)
    installed = agent_install_mod.install(_here(project), qualified_name, pin=pin, track=track)
    typer.echo(
        f"added {qualified_name} {installed.current.identifier()} -> {installed.target_path}"
    )
    for warn in agent_install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)


@subagent_app.command("install", hidden=True)
@_friendly
def agent_install_cmd(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<agent_name>"),
    project: Path | None = typer.Argument(None, help="Project root."),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None,
        "--track",
        help="Ref to track on update: 'latest-tag', a branch name, or any ref. Overrides repo default_ref.",
    ),
) -> None:
    """Deprecated: install an already-registered sub-agent by qualified name. Use `add`."""
    installed = agent_install_mod.install(_here(project), qualified_name, pin=pin, track=track)
    typer.echo(
        f"installed {qualified_name} {installed.current.identifier()} -> {installed.target_path}"
    )
    for warn in agent_install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)


@subagent_app.command("update")
@_friendly
def agent_update(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Refresh an installed sub-agent from its source repo."""
    updated = agent_install_mod.update(_here(project), qualified_name, force=force)
    typer.echo(f"updated {qualified_name} -> {updated.current.identifier()}")


@subagent_app.command("update-many")
@_friendly
def agent_update_many(
    project: Path | None = typer.Argument(None),
    all_agents: bool = typer.Option(False, "--all", help="Update every installed agent."),
    repo: str | None = typer.Option(None, "--repo", help="Limit to a single repo alias."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip agents already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Update installed sub-agents in bulk."""
    if not all_agents and repo is None:
        raise typer.BadParameter("pass --all or --repo <alias>")
    outcomes = agent_install_mod.update_many(
        _here(project),
        repo_alias=repo,
        only_outdated=only_outdated,
        force=force,
    )
    for o in outcomes:
        typer.echo(f"{o['status']:>12}  {o['qualified_name']}  {o['detail']}")
    errors = [o for o in outcomes if o["status"] == "error"]
    if errors:
        raise typer.Exit(code=1)


@subagent_app.command("remove")
@_friendly
def agent_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<agent_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed sub-agent from the project."""
    agent_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed {qualified_name}")


@subagent_app.command("uninstall", hidden=True)
@_friendly
def agent_uninstall(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    agent_remove(qualified_name, project)


@subagent_app.command("delete", hidden=True)
@_friendly
def agent_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    agent_remove(qualified_name, project)


@subagent_app.command("rollback")
@_friendly
def agent_rollback(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a sub-agent."""
    rolled = agent_install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back {qualified_name} -> {rolled.current.identifier()}")


def _parse_key_value_list(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"expected NAME=VALUE, got {item!r}")
        name, _, value = item.partition("=")
        out[name] = value
    return out


def _parse_header_list(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            raise typer.BadParameter(f"expected Name:Value, got {item!r}")
        name, _, value = item.partition(":")
        out[name.strip()] = value.strip()
    return out


if __name__ == "__main__":
    app()
