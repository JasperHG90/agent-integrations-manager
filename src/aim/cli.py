"""Typer entry point. CLI is the surface for scripting/CI; the TUI uses the
same core API.
"""

from __future__ import annotations

import asyncio
import functools
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
from aim.core import repos as repos_mod
from aim.core import roots as roots_mod
from aim.core import rule_repos as rule_repos_mod
from aim.core import rules as rules_mod
from aim.core import skills as skills_mod
from aim.core import sync as sync_mod
from aim.core import templates as templates_mod

app = typer.Typer(
    add_completion=False,
    help="Scaffold agent-engineering projects. Run with no arguments to launch the TUI.",
    invoke_without_command=True,
)
rule_app = typer.Typer(no_args_is_help=True, help="Manage the global rule library.")
repo_app = typer.Typer(no_args_is_help=True, help="Manage skill source repositories.")
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
    rules_mod.RuleNameError,
    rules_mod.RuleNotFoundError,
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
    rule_repos_mod.RuleRepoAliasError,
    rule_repos_mod.RuleRepoExistsError,
    rule_repos_mod.RuleRepoNotFoundError,
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
        help="Allow plain http:// transports for repos, rule-repos, and MCP servers.",
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
def root_remove(path: Path = typer.Argument(...)) -> None:
    removed = roots_mod.remove_root(path.expanduser())
    typer.echo(f"removed {path}" if removed else f"not in roots: {path}")


rule_repo_app = typer.Typer(
    no_args_is_help=True, help="Manage shared rule library overlays (git-backed)."
)
app.add_typer(rule_repo_app, name="rule-repo")


@rule_repo_app.command("add")
@_friendly
def rule_repo_add(
    ctx: typer.Context,
    alias: str = typer.Argument(...),
    url: str = typer.Argument(...),
    default_ref: str = typer.Option("HEAD", "--ref"),
) -> None:
    entry = rule_repos_mod.add(
        alias, url, default_ref=default_ref, allow_insecure=_get_allow_insecure(ctx)
    )
    typer.echo(f"added rule-repo {entry.alias} -> {entry.url}")


@rule_repo_app.command("list")
@_friendly
def rule_repo_list(ctx: typer.Context) -> None:
    """List registered rule library overlays."""
    entries = rule_repos_mod.list_repos()
    format_mod.render(
        entries,
        _get_format(ctx),
        title="rule-repos registered",
        columns=["alias", "url", "default_ref", "head"],
        row_extractor={
            "alias": "alias",
            "url": "url",
            "default_ref": "default_ref",
            "head": "last_sha",
        },
        compact_columns=["alias", "url", "default_ref"],
    )


@rule_repo_app.command("refresh")
@_friendly
def rule_repo_refresh(
    ctx: typer.Context,
    alias: str,
) -> None:
    entry = rule_repos_mod.refresh(alias, allow_insecure=_get_allow_insecure(ctx))
    typer.echo(f"refreshed {alias}: HEAD={(entry.last_sha or '?')[:12]}")


@rule_repo_app.command("remove")
@_friendly
def rule_repo_remove(alias: str) -> None:
    rule_repos_mod.remove(alias)
    typer.echo(f"removed rule-repo {alias}")


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
    p = profiles_mod.load(name)
    typer.echo(p.model_dump_json(indent=2))


@profile_app.command("delete")
@_friendly
def profile_delete(name: str) -> None:
    removed = profiles_mod.delete(name)
    typer.echo(f"deleted {name}" if removed else f"not found: {name}")


@profile_app.command("apply")
@_friendly
def profile_apply(
    ctx: typer.Context,
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
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


@mcp_app.command("install")
@_friendly
def mcp_install_cmd(
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
    """Install an MCP server into the project's .mcp.json."""
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
    typer.echo(f"installed MCP server {installed.registry_name} as {installed.alias}")


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


@mcp_app.command("uninstall")
@_friendly
def mcp_uninstall_cmd(
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Remove a managed MCP server from .mcp.json."""
    mcp_install_mod.delete(_here(project), alias)
    typer.echo(f"uninstalled MCP server {alias}")


@mcp_app.command("delete", hidden=True)
@_friendly
def mcp_delete_cmd(
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Deprecated alias for "uninstall"."""
    mcp_uninstall_cmd(alias, project)


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
    rule: list[str] = typer.Option(
        [], "--rule", "-r", help="Additional rule name to apply (repeatable)."
    ),
    rule_file: list[str] = typer.Option(
        [],
        "--rule-file",
        help="Seed a rule from FILE. Format name=path or just path (stem becomes name). Repeatable.",
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", help="Layout profile to use (overrides manifest)."
    ),
) -> None:
    """Create or update the user-editable aim.toml declarations file."""
    extra_rule_files: dict[str, Path] = {}
    for rf in rule_file:
        if "=" in rf:
            name, _, path_str = rf.partition("=")
        else:
            path_str = rf
            name = Path(path_str).stem
        if not name:
            raise typer.BadParameter(f"rule-file {rf!r} has no name")
        path = Path(path_str).expanduser()
        if not path.is_file():
            raise typer.BadParameter(f"rule-file not found: {path}")
        extra_rule_files[name] = path

    options = init_mod.InitOptions(
        project_root=_here(project),
        instruction_template=instruction_template,
        symlinks=tuple(symlink),
        extra_rules=list(rule),
        extra_rule_files=extra_rule_files,
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
        False, "--dry-run", "-n", help="Preview removals without deleting."
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", help="Layout profile to use (overrides manifest)."
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="Glob pattern to protect from pruning (can be repeated)."
    ),
) -> None:
    """Remove skills/agents/rules/MCP servers not listed in aim.lock.toml.

    Persistent exclusions can also be stored in an `.aimignore` file at the
    project root with one glob pattern per line, e.g. `.claude/skills/local/*`.
    """
    console = Console()
    with console.status("Pruning artifacts...", spinner="dots"):
        result = prune_mod.run(
            prune_mod.PruneOptions(
                project_root=_here(project),
                dry_run=dry_run,
                layout_profile=layout_profile,
                excludes=list(exclude),
            )
        )
    for item in result.removed:
        prefix = "would remove" if item.action == "would-remove" else item.action
        typer.echo(f"{prefix} {item.kind} {item.path}", err=item.action != "removed")
    for item in result.kept:
        typer.echo(f"{item.action} {item.kind} {item.path}")


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


@rule_app.command("add")
@_friendly
def rule_add(
    name: str = typer.Argument(..., help="Rule name (lowercase, alphanumeric + - _)."),
    body_file: Path | None = typer.Option(
        None,
        "--from",
        "-f",
        help="Read rule body from FILE (defaults to '-' for stdin).",
    ),
    body: str | None = typer.Option(None, "--body", "-b", help="Inline rule body."),
    description: str | None = typer.Option(None, "--description", "-d"),
    default: bool = typer.Option(
        False, "--default", help="Mark as a global default (seeded by `init`)."
    ),
) -> None:
    """Add or replace a rule in the global library."""
    text = _resolve_body(body, body_file)
    rule = rules_mod.add(name, text, description=description, is_default=default)
    flag = " [default]" if rule.is_default else ""
    typer.echo(f"added rule {rule.name}{flag}")


@rule_app.command("list")
@_friendly
def rule_list(ctx: typer.Context) -> None:
    """List rules in the global library."""
    entries = rules_mod.list_all()
    rows = [
        {"name": r.name, "default": r.is_default, "source": r.source, "description": r.description}
        for r in entries
    ]
    format_mod.render(
        rows,
        _get_format(ctx),
        title="rules registered",
        columns=["name", "default", "source", "description"],
        compact_columns=["name", "default", "source", "description"],
    )


@rule_app.command("edit")
@_friendly
def rule_edit(name: str) -> None:
    """Print the path to the rule body file (use $EDITOR on the result)."""
    rule = rules_mod.get(name)
    typer.echo(rules_mod.body_path(rule.name))


@rule_app.command("set-default")
@_friendly
def rule_set_default(
    name: str,
    enable: bool = typer.Option(
        True,
        "--default/--no-default",
        help="Flag (or unflag) the rule as a global default.",
    ),
) -> None:
    rules_mod.set_default(name, is_default=enable)
    state = "default" if enable else "not default"
    typer.echo(f"{name}: {state}")


@rule_app.command("delete")
@_friendly
def rule_delete(name: str) -> None:
    rules_mod.delete(name)
    typer.echo(f"deleted rule {name}")


@rule_app.command("apply")
@_friendly
def rule_apply(
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Low-level: copy rule body into a project's .aim/rules/ dir
    without touching the manifest. Use `install` for the full flow."""
    rules_mod.apply_to_project(_here(project), [name])
    typer.echo(f"applied {name} to {_here(project)}")


@rule_app.command("install")
@_friendly
def rule_install(
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Add a rule to a project's aim.toml declarations."""
    result = rules_mod.install_to_project(_here(project), name)
    typer.echo(f"added rule {name} to {result.project_root}/aim.toml")
    typer.echo("Run `aim lock` and `aim sync` to apply the change to disk.")


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
    repos_mod.remove(alias)
    typer.echo(f"removed repo {alias}")


@repo_app.command("rename")
@_friendly
def repo_rename(old: str, new: str) -> None:
    repos_mod.rename(old, new)
    typer.echo(f"renamed {old} -> {new}")


@repo_app.command("refresh")
@_friendly
def repo_refresh(
    ctx: typer.Context,
    alias: str,
) -> None:
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


@skill_app.command("install")
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


@skill_app.command("uninstall")
@_friendly
def skill_uninstall(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Remove an installed skill from the project."""
    install_mod.delete(_here(project), qualified_name)
    typer.echo(f"uninstalled {qualified_name}")


@skill_app.command("delete", hidden=True)
@_friendly
def skill_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "uninstall"."""
    skill_uninstall(qualified_name, project)


@skill_app.command("rollback")
@_friendly
def skill_rollback(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
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


@subagent_app.command("install")
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


@subagent_app.command("uninstall")
@_friendly
def agent_uninstall(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Remove an installed sub-agent from the project."""
    agent_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"uninstalled {qualified_name}")


@subagent_app.command("delete", hidden=True)
@_friendly
def agent_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "uninstall"."""
    agent_uninstall(qualified_name, project)


@subagent_app.command("rollback")
@_friendly
def agent_rollback(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
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


def _resolve_body(body: str | None, body_file: Path | None) -> str:
    if body is not None and body_file is not None:
        raise typer.BadParameter("pass --body or --from, not both")
    if body is not None:
        return body
    if body_file is not None:
        if str(body_file) == "-":
            import sys

            return sys.stdin.read()
        return body_file.read_text()
    raise typer.BadParameter("must pass --body or --from")


if __name__ == "__main__":
    app()
