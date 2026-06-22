"""`aim subagent`: discover and manage sub-agents."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import (
    _friendly,
    _get_format,
    _here,
    _qualified_for_add,
    _run_bulk_update,
    _scanning,
)
from aim.core import agent_install as agent_install_mod
from aim.core import agents as agents_mod
from aim.core import format as format_mod
from aim.core import risk as risk_mod

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Discover and manage sub-agents."
)


@app.command("list")
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


@app.command("search")
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


@app.command("view")
@_friendly
def agent_view(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<agent_name> to display."),
) -> None:
    """Print an indexed sub-agent's AGENT.md source."""
    typer.echo(agents_mod.read_agent_content(qualified_name))


@app.command("add")
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
    override_risk: bool = typer.Option(
        False,
        "--override-risk",
        help="Install despite a risk block (unless the policy forbids it).",
    ),
) -> None:
    """Add a sub-agent from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "sub-agent", assume_yes=yes)
    risk_mod.prewarm(_here(project))  # overlap model load with the clone below
    with _scanning(f"Scanning {qualified_name}…"):
        installed = agent_install_mod.install(
            _here(project), qualified_name, pin=pin, track=track, override_risk=override_risk
        )
    typer.echo(
        f"added {qualified_name} {installed.current.identifier()} -> {installed.target_path}"
    )
    for warn in agent_install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


@app.command("install", hidden=True)
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
    override_risk: bool = typer.Option(
        False,
        "--override-risk",
        help="Install despite a risk block (unless the policy forbids it).",
    ),
) -> None:
    """Deprecated: install an already-registered sub-agent by qualified name. Use `add`."""
    installed = agent_install_mod.install(
        _here(project), qualified_name, pin=pin, track=track, override_risk=override_risk
    )
    typer.echo(
        f"installed {qualified_name} {installed.current.identifier()} -> {installed.target_path}"
    )
    for warn in agent_install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


@app.command("update")
@_friendly
def agent_update(
    qualified_name: str | None = typer.Argument(
        None, help="<repo_alias>/<agent_name>; omit and use --all/--repo for bulk."
    ),
    project: Path | None = typer.Argument(None),
    all_agents: bool = typer.Option(False, "--all", help="Update every installed sub-agent."),
    repo: str | None = typer.Option(None, "--repo", help="Update only this repo's sub-agents."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip agents already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Update despite a risk block (unless the policy forbids it)."
    ),
) -> None:
    """Refresh an installed sub-agent, or update in bulk with --all / --repo."""
    if qualified_name is not None:
        updated = agent_install_mod.update(
            _here(project), qualified_name, force=force, override_risk=override_risk
        )
        typer.echo(f"updated {qualified_name} -> {updated.current.identifier()}")
        return
    if not all_agents and repo is None:
        raise typer.BadParameter("pass a <name>, --all, or --repo <alias>")
    _run_bulk_update(
        agent_install_mod.update_many, project, repo, only_outdated, force, override_risk
    )


@app.command("remove")
@_friendly
def agent_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<agent_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed sub-agent from the project."""
    agent_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed {qualified_name}")


@app.command("uninstall", hidden=True)
@_friendly
def agent_uninstall(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    agent_remove(qualified_name, project)


@app.command("delete", hidden=True)
@_friendly
def agent_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    agent_remove(qualified_name, project)


@app.command("rollback")
@_friendly
def agent_rollback(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a sub-agent."""
    rolled = agent_install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back {qualified_name} -> {rolled.current.identifier()}")
