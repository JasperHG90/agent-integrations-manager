"""Typer entry point. CLI is the surface for scripting/CI; the TUI uses the
same core API.

Command groups are registered lazily (see `aim.cli._lazy.LazyTyperGroup`) so importing
this module — which happens on every invocation, including bare `aim` for the TUI — does
not eagerly import the whole `aim.core` surface. Top-level commands stay here but import
their heavy dependencies inside their bodies for the same reason.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from aim import __version__
from aim.cli._lazy import LAZY_HIDDEN, LAZY_SUBCOMMANDS, LazyTyperGroup
from aim.cli._shared import (
    _friendly,
    _get_allow_insecure,
    _here,
    _looks_like_url,
    _parse_source_url,
)
from aim.core import format as format_mod

app = typer.Typer(
    cls=LazyTyperGroup,
    add_completion=False,
    help="Scaffold agent-engineering projects. Run with no arguments to launch the TUI.",
    invoke_without_command=True,
)

# Lazily-loaded command groups, in the order they appear in `--help`.
LAZY_SUBCOMMANDS.update(
    {
        "rule": "aim.cli.commands.rule:app",
        "repo": "aim.cli.commands.repo:app",
        "skill": "aim.cli.commands.skill:app",
        "subagent": "aim.cli.commands.subagent:app",
        "archetype": "aim.cli.commands.instructions:app",
        "db": "aim.cli.commands.db:app",
        "root": "aim.cli.commands.root:app",
        "template": "aim.cli.commands.profile:app",
        "policy": "aim.cli.commands.policy:app",
        "mcp": "aim.cli.commands.mcp:app",
        # Back-compat aliases; dispatch but hidden from --help.
        "profile": "aim.cli.commands.profile:app",
        "instructions": "aim.cli.commands.instructions:app",
    }
)
LAZY_HIDDEN.add("profile")
LAZY_HIDDEN.add("instructions")

# Re-exported for tests that import these private helpers by path.
__all__ = ["_looks_like_url", "_parse_source_url", "app"]


def _version_callback(value: bool) -> None:
    """Print the version and exit when `--version` is passed."""
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
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            typer.echo(ctx.get_help())
            raise typer.Exit(code=2)
        from aim.tui.app import run as run_tui

        run_tui(project_root=project, profile_name=profile)
        raise typer.Exit()


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
    from aim.core import agents_md as agents_md_mod
    from aim.core import hashing
    from aim.core import manifest as manifest_mod
    from aim.core import mcp_registry as mcp_registry_mod

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
    from aim.core import doctor as doctor_mod

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


def _maybe_setup_policy(root: Path, policy_url: str | None, local_policy: bool) -> None:
    """Configure governance at init by writing the [policy] table in aim.toml.
    `aim init` already wrote a default scope='local'; here we optionally switch to an
    org policy (flag or interactive prompt)."""
    from aim.core import policy as policy_mod

    if policy_url:
        resolved = policy_mod.bind(policy_url)  # fetch + cache
        policy_mod.set_project_policy(root, {"scope": "org", "repo": policy_url, "ref": "HEAD"})
        typer.echo(f"bound org policy {resolved.policy.name!r} ({policy_url})")
        return
    if local_policy:
        typer.echo("using local policy ([policy] scope='local' in aim.toml)")
        return
    section = policy_mod.project_policy_section(root)
    if section.get("scope") == "org" or not sys.stdin.isatty():
        return
    if typer.confirm("Bind an org governance policy repo?", default=False):
        url = typer.prompt("Org policy git URL")
        resolved = policy_mod.bind(url)
        policy_mod.set_project_policy(root, {"scope": "org", "repo": url, "ref": "HEAD"})
        typer.echo(f"bound org policy {resolved.policy.name!r}")


def _prompt_instructions() -> str | None:
    """Interactively choose a project-instruction archetype, or the built-in template.

    Returns:
        The chosen archetype's qualified name, `init_mod.BUILTIN_INSTRUCTIONS` for the
        built-in template, or None when no archetypes are available to choose from.
    """
    from aim.core import archetypes as archetypes_mod
    from aim.core import init as init_mod

    rows = archetypes_mod.list_archetypes()
    if not rows:
        return None
    typer.echo("Choose project instructions:")
    typer.echo("  0) built-in template (default)")
    for index, row in enumerate(rows, start=1):
        suffix = f" — {row.title}" if row.title else ""
        typer.echo(f"  {index}) {row.qualified_name}{suffix}")
    selection = typer.prompt("Selection", default="0")
    try:
        chosen = int(selection)
    except ValueError:
        chosen = 0
    if 1 <= chosen <= len(rows):
        return rows[chosen - 1].qualified_name
    return init_mod.BUILTIN_INSTRUCTIONS


@app.command("init")
@_friendly
def init_cmd(
    project: Path | None = typer.Argument(None, help="Project root (default: current directory)."),
    symlink: list[str] = typer.Option(
        [],
        "--symlink",
        help="Symlink to create pointing at AGENTS.md (repeatable, e.g. CLAUDE.md).",
    ),
    layout_profile: str | None = typer.Option(
        None, "--profile", help="Layout profile to use (overrides manifest)."
    ),
    instructions: str | None = typer.Option(
        None,
        "--archetype",
        "--instructions",
        help="Instruction archetype '<alias>/<name>', or 'builtin' for the default template.",
    ),
    policy_url: str | None = typer.Option(
        None, "--policy", help="Bind to this org governance policy repo."
    ),
    local_policy: bool = typer.Option(
        False, "--local-policy", help="Create/use a local editable policy."
    ),
) -> None:
    """Create or update the user-editable aim.toml declarations file.

    Rules are repo-sourced; add them after init with `aim rule add <git-url> <name>`.
    """
    from aim.core import init as init_mod

    root = _here(project)
    chosen = instructions
    if chosen is None and sys.stdin.isatty() and sys.stdout.isatty():
        chosen = _prompt_instructions()
    options = init_mod.InitOptions(
        project_root=root,
        symlinks=tuple(symlink),
        layout_profile=layout_profile,
        instruction_archetype=chosen,
    )
    result = init_mod.run(options)
    _maybe_setup_policy(root, policy_url, local_policy)
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
    no_index: bool = typer.Option(
        False,
        "--no-index",
        help="Skip refreshing the skill/agent search index; only resolve declared "
        "artifacts. Much faster, but `aim ... search` results may be stale or incomplete.",
    ),
) -> None:
    """Resolve aim.toml declarations into an exact aim.lock.toml."""
    import asyncio

    from aim.core import lock as lock_mod

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
                no_index=no_index,
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
    import asyncio

    from aim.core import sync as sync_mod

    console = Console()

    async def _run(status: Any) -> sync_mod.SyncResult:
        def _progress(kind: str, name: str, state: str) -> None:
            status.update(f"{kind} {name}: {state}")

        return await sync_mod.run(
            sync_mod.SyncOptions(
                project_root=_here(project),
                force=force,
                sync_agents=not no_sync_agents,
                layout_profile=layout_profile,
                allow_insecure=_get_allow_insecure(ctx),
                progress_callback=_progress,
            )
        )

    with console.status("Syncing...", spinner="dots") as status:
        result = asyncio.run(_run(status))
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
    from aim.core import prune as prune_mod

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


def _print_prune_apply_result(console: Console, result: Any) -> None:
    """Print removed entries, skipped/error items, and warnings from a prune."""
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
    """Print a unified diff for each change's before/after text."""
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


if __name__ == "__main__":
    app()
