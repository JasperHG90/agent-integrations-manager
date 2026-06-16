"""Typer entry point. CLI is the surface for scripting/CI; the TUI uses the
same core API.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from agent_init import __version__
from agent_init.core import agent_install as agent_install_mod
from agent_init.core import agents as agents_mod
from agent_init.core import doctor as doctor_mod
from agent_init.core import format as format_mod
from agent_init.core import git
from agent_init.core import init as init_mod
from agent_init.core import install as install_mod
from agent_init.core import mcp_install as mcp_install_mod
from agent_init.core import mcp_registry as mcp_registry_mod
from agent_init.core import profiles as profiles_mod
from agent_init.core import repos as repos_mod
from agent_init.core import roots as roots_mod
from agent_init.core import rule_repos as rule_repos_mod
from agent_init.core import rules as rules_mod
from agent_init.core import skills as skills_mod
from agent_init.core import templates as templates_mod

app = typer.Typer(
    add_completion=False,
    help="Scaffold agent-engineering projects. Run with no arguments to launch the TUI.",
    invoke_without_command=True,
)
rule_app = typer.Typer(no_args_is_help=True, help="Manage the global rule library.")
repo_app = typer.Typer(no_args_is_help=True, help="Manage skill source repositories.")
skill_app = typer.Typer(no_args_is_help=True, help="Discover and manage skills.")
agent_app = typer.Typer(no_args_is_help=True, help="Discover and manage sub-agents.")
app.add_typer(rule_app, name="rule")
app.add_typer(repo_app, name="repo")
app.add_typer(skill_app, name="skill")
app.add_typer(agent_app, name="agent")


from agent_init.core import manifest as manifest_mod  # noqa: E402

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
    git.GitError,
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
        typer.echo(f"agent-init {__version__}")
        raise typer.Exit()


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
) -> None:
    """agent-init: scaffold and manage agent-engineering projects.

    With no subcommand, launches the Textual TUI — the primary surface.
    Subcommands are available for scripting/CI.
    """
    if ctx.invoked_subcommand is None:
        import sys

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            typer.echo(ctx.get_help())
            raise typer.Exit(code=2)
        from agent_init.tui.app import run as run_tui

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
            from agent_init.core import agents_md, hashing, manifest

            m = manifest.load(proj)
        except manifest.ManifestNotFoundError:
            typer.echo(f"{proj}: no manifest (skipped)", err=True)
            continue
        for managed in m.managed_files:
            target = proj / managed
            if not target.exists():
                typer.echo(f"{proj}/{managed}: missing", err=True)
                bad += 1
                continue
            try:
                regions = agents_md.parse(target.read_text())
            except agents_md.RegionError as exc:
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
def root_list() -> None:
    entries = roots_mod.list_roots()
    if not entries:
        typer.echo("no roots configured")
        return
    for r in entries:
        typer.echo(str(r))


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
    alias: str = typer.Argument(...),
    url: str = typer.Argument(...),
    default_ref: str = typer.Option("HEAD", "--ref"),
) -> None:
    entry = rule_repos_mod.add(alias, url, default_ref=default_ref)
    typer.echo(f"added rule-repo {entry.alias} -> {entry.url}")


@rule_repo_app.command("list")
@_friendly
def rule_repo_list() -> None:
    entries = rule_repos_mod.list_repos()
    if not entries:
        typer.echo("no rule-repos registered")
        return
    for r in entries:
        typer.echo(f"{r.alias}\t{r.url}\tHEAD={(r.last_sha or '?')[:12]}")


@rule_repo_app.command("refresh")
@_friendly
def rule_repo_refresh(alias: str) -> None:
    entry = rule_repos_mod.refresh(alias)
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
def profile_list() -> None:
    entries = profiles_mod.list_profiles()
    if not entries:
        typer.echo("no profiles saved")
        return
    for p in entries:
        mirrors = ",".join(p.mirrors) if p.mirrors else "-"
        skills_n = len(p.skills)
        agents_n = len(p.agents)
        mcp_n = len(p.mcp_servers)
        rules_n = len(p.rules)
        typer.echo(
            f"{p.name}\ttemplate={p.template}\tmirrors={mirrors}\t"
            f"skills={skills_n}\tagents={agents_n}\tmcp={mcp_n}\trules={rules_n}"
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
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    result = profiles_mod.apply(name, _here(project))
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
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """List MCP servers installed in the project."""
    m = manifest_mod.load_or_default(_here(project))
    if not m.mcp_servers:
        typer.echo("no MCP servers installed")
        return
    for s in m.mcp_servers:
        typer.echo(f"{s.alias}\t{s.registry_name}\t{s.current.registry_version or '?'}")


@mcp_app.command("install")
@_friendly
def mcp_install_cmd(
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
    )
    typer.echo(f"installed MCP server {installed.registry_name} as {installed.alias}")


@mcp_app.command("update")
@_friendly
def mcp_update_cmd(
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Refresh a managed MCP server from the registry."""
    updated = mcp_install_mod.update(_here(project), alias, force=force)
    typer.echo(f"updated MCP server {updated.alias} -> {updated.current.registry_version or '?'}")


@mcp_app.command("delete")
@_friendly
def mcp_delete_cmd(
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Remove a managed MCP server from .mcp.json."""
    mcp_install_mod.delete(_here(project), alias)
    typer.echo(f"deleted MCP server {alias}")


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
    from agent_init.tui.app import run as run_tui

    run_tui(project_root=project, profile_name=profile)


@app.command("init")
@_friendly
def init_cmd(
    project: Path | None = typer.Argument(None, help="Project root (default: current directory)."),
    template: str = typer.Option(
        templates_mod.BUILTIN_DEFAULT, "--template", "-t", help="Template name."
    ),
    mirror: list[str] = typer.Option(
        [],
        "--mirror",
        "-m",
        help="Mirror file to write alongside AGENTS.md (repeatable, e.g. CLAUDE.md). Opt-in.",
    ),
    no_default_rules: bool = typer.Option(
        False, "--no-default-rules", help="Skip seeding rules flagged as default."
    ),
    rule: list[str] = typer.Option(
        [], "--rule", "-r", help="Additional rule name to apply (repeatable)."
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing AGENTS.md / mirrors."
    ),
    diff: bool = typer.Option(
        False, "--diff", help="Print pending changes as a unified diff; don't write."
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", "-p", help="Layout profile to use (overrides manifest)."
    ),
) -> None:
    """Initialize or refresh agent-init scaffolding in PROJECT."""
    options = init_mod.InitOptions(
        project_root=_here(project),
        template=template,
        mirrors=tuple(mirror),
        seed_default_rules=not no_default_rules,
        extra_rules=list(rule),
        force=force,
        dry_run=diff,
        layout_profile=layout_profile,
    )
    result = init_mod.run(options)
    if diff:
        _print_diffs(result.pending_changes)
        typer.echo(
            f"(dry-run: {len(result.pending_changes)} file(s) would change; pass without --diff to apply)"
        )
        return
    verb = "Refreshed" if result.re_init else "Initialized"
    typer.echo(f"{verb} {result.agents_md_path}")
    for mp in result.mirror_paths:
        typer.echo(f"  mirror: {mp}")
    for sp in result.symlink_paths:
        typer.echo(f"  symlink: {sp}")
    if result.applied_rules:
        typer.echo(f"  rules:  {', '.join(result.applied_rules)}")
    if result.region_drift_warnings:
        for warn in result.region_drift_warnings:
            typer.echo(f"  warning: {warn}", err=True)
    typer.echo(f"  manifest: {result.manifest_path}")


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
def rule_list() -> None:
    """List rules in the global library."""
    entries = rules_mod.list_all()
    if not entries:
        typer.echo("no rules registered")
        return
    for r in entries:
        flag = "*" if r.is_default else " "
        desc = f" — {r.description}" if r.description else ""
        typer.echo(f"{flag} {r.name}{desc}")
    typer.echo("\n(* = global default; auto-seeded by `init`)")


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
    """Low-level: copy rule body into a project's .agent-init/rules/ dir
    without touching the manifest. Use `install` for the full flow."""
    rules_mod.apply_to_project(_here(project), [name])
    typer.echo(f"applied {name} to {_here(project)}")


@rule_app.command("install")
@_friendly
def rule_install(
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Install a rule into a project: copy body, update manifest, re-render AGENTS.md."""
    result = rules_mod.install_to_project(_here(project), name)
    typer.echo(f"installed rule {name} into {result.project_root}")
    if result.region_drift_warnings:
        for warn in result.region_drift_warnings:
            typer.echo(f"  warning: {warn}", err=True)


# ---------- repo ----------


@repo_app.command("add")
@_friendly
def repo_add(
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
    repo = repos_mod.add(alias, url, default_ref=default_ref, allow_empty=allow_empty)
    typer.echo(f"added repo {repo.alias} -> {repo.url}")
    if repo.last_sha:
        typer.echo(f"  HEAD: {repo.last_sha[:12]}")


@repo_app.command("list")
@_friendly
def repo_list() -> None:
    repos = repos_mod.list_repos()
    if not repos:
        typer.echo("no repos registered")
        return
    for r in repos:
        sha = r.last_sha[:12] if r.last_sha else "?"
        when = r.last_fetched_at.isoformat() if r.last_fetched_at else "?"
        typer.echo(f"{r.alias}\t{r.url}\t{sha}\tlast_fetched={when}")


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
def repo_refresh(alias: str) -> None:
    repo = repos_mod.refresh(alias)
    sha = repo.last_sha[:12] if repo.last_sha else "?"
    typer.echo(f"refreshed {alias}: HEAD={sha}")


# ---------- skill ----------


@skill_app.command("list")
@_friendly
def skill_list(
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    rows = skills_mod.list_skills(repo)
    if not rows:
        typer.echo("no skills indexed")
        return
    for row in rows:
        desc = f" — {row.description}" if row.description else ""
        typer.echo(f"{row.qualified_name}{desc}")


@skill_app.command("search")
@_friendly
def skill_search(query: str = typer.Argument(..., help="Substring to match.")) -> None:
    rows = skills_mod.search(query)
    if not rows:
        typer.echo(f"no skills match {query!r}")
        return
    for row in rows:
        desc = f" — {row.description}" if row.description else ""
        typer.echo(f"{row.qualified_name}{desc}")


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


@skill_app.command("delete")
@_friendly
def skill_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    install_mod.delete(_here(project), qualified_name)
    typer.echo(f"deleted {qualified_name}")


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


@agent_app.command("list")
@_friendly
def agent_list(
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    rows = agents_mod.list_agents(repo)
    if not rows:
        typer.echo("no agents indexed")
        return
    for row in rows:
        desc = f" — {row.description}" if row.description else ""
        typer.echo(f"{row.qualified_name}{desc}")


@agent_app.command("search")
@_friendly
def agent_search(query: str = typer.Argument(..., help="Substring to match.")) -> None:
    rows = agents_mod.search(query)
    if not rows:
        typer.echo(f"no agents match {query!r}")
        return
    for row in rows:
        desc = f" — {row.description}" if row.description else ""
        typer.echo(f"{row.qualified_name}{desc}")


@agent_app.command("install")
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


@agent_app.command("update")
@_friendly
def agent_update(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    updated = agent_install_mod.update(_here(project), qualified_name, force=force)
    typer.echo(f"updated {qualified_name} -> {updated.current.identifier()}")


@agent_app.command("update-many")
@_friendly
def agent_update_many(
    project: Path | None = typer.Argument(None),
    all_agents: bool = typer.Option(False, "--all", help="Update every installed agent."),
    repo: str | None = typer.Option(None, "--repo", help="Limit to a single repo alias."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip agents already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Update installed agents in bulk."""
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


@agent_app.command("delete")
@_friendly
def agent_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    agent_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"deleted {qualified_name}")


@agent_app.command("rollback")
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
