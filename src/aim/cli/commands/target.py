"""`aim target`: discover and manage repo-sourced plugin targets.

A target is a declarative plugin-kind TOML shared via a git repo. Installing one
vendors it into the project's ``.aim/targets/`` (SHA-pinned in ``aim.lock.toml``),
so a teammate's ``aim sync`` reproduces it. Targets are config, not agent-facing
instructions, so they are not risk-scanned.
"""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_format, _here, _qualified_for_add, _scanning
from aim.core import format as format_mod
from aim.core import target_install as target_install_mod
from aim.core import targets as targets_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Discover and manage repo-sourced plugin targets.",
)

_COLUMNS = ["qualified_name", "repo_alias", "title", "description"]
_COMPACT = ["qualified_name", "title", "description"]


@app.command("list")
@_friendly
def target_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    """List indexed plugin targets."""
    format_mod.render(
        targets_mod.list_targets(repo),
        _get_format(ctx),
        title="targets indexed",
        columns=_COLUMNS,
        compact_columns=_COMPACT,
    )


@app.command("search")
@_friendly
def target_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Substring to match."),
) -> None:
    """Search indexed plugin targets by substring."""
    format_mod.render(
        targets_mod.search(query),
        _get_format(ctx),
        title=f"targets matching {query!r}",
        columns=_COLUMNS,
        compact_columns=_COMPACT,
    )


@app.command("view")
@_friendly
def target_view(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<target_name> to display."),
) -> None:
    """Print an indexed target's source TOML."""
    typer.echo(targets_mod.read_target_content(qualified_name))


@app.command("add")
@_friendly
def target_add(
    ctx: typer.Context,
    url: str = typer.Argument(
        ...,
        help="Git URL (clone or web tree/blob), or '<alias>/<name>' of an already-registered repo.",
    ),
    name: str | None = typer.Argument(
        None, help="Target name within the repo (inferred from a tree/blob URL if omitted)."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    alias: str | None = typer.Option(
        None, "--alias", help="Repo alias to register under (default: derived from the URL)."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None, "--track", help="Ref to track on update: 'latest-tag', a branch name, or any ref."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Register the source repo without prompting."
    ),
) -> None:
    """Add a plugin target from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "target", assume_yes=yes)
    with _scanning(f"Installing {qualified_name}…"):
        installed = target_install_mod.install(_here(project), qualified_name, pin=pin, track=track)
    typer.echo(f"added target {qualified_name} {installed.current.identifier()}")


@app.command("update")
@_friendly
def target_update(
    qualified_name: str | None = typer.Argument(
        None, help="<repo_alias>/<target_name>; omit and use --all/--repo for bulk."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    all_targets: bool = typer.Option(False, "--all", help="Update every installed target."),
    repo: str | None = typer.Option(None, "--repo", help="Update only this repo's targets."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip targets already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Refresh an installed target, or update in bulk with --all / --repo."""
    if qualified_name is not None:
        updated = target_install_mod.update(_here(project), qualified_name, force=force)
        typer.echo(f"updated target {qualified_name} -> {updated.current.identifier()}")
        return
    if not all_targets and repo is None:
        raise typer.BadParameter("pass a <name>, --all, or --repo <alias>")
    outcomes = target_install_mod.update_many(
        _here(project), repo_alias=repo, only_outdated=only_outdated, force=force
    )
    for outcome in outcomes:
        typer.echo(f"{outcome['status']:>12}  {outcome['qualified_name']}  {outcome['detail']}")
    if any(outcome["status"] == "error" for outcome in outcomes):
        raise typer.Exit(code=1)


@app.command("remove")
@_friendly
def target_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<target_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed target from the project."""
    target_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed target {qualified_name}")


@app.command("rollback")
@_friendly
def target_rollback(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<target_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a target."""
    rolled = target_install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back target {qualified_name} -> {rolled.current.identifier()}")
