"""`aim rule`: discover and manage repo-sourced rules."""

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
from aim.core import format as format_mod
from aim.core import repo_rules as repo_rules_mod
from aim.core import risk as risk_mod
from aim.core import rule_install as rule_install_mod

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Discover and manage repo-sourced rules."
)


@app.command("list")
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


@app.command("search")
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


@app.command("view")
@_friendly
def rule_view(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<rule_name> to display."),
) -> None:
    """Print an indexed rule's source markdown."""
    typer.echo(repo_rules_mod.read_rule_content(qualified_name))


@app.command("add")
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
    override_risk: bool = typer.Option(
        False,
        "--override-risk",
        help="Install despite a risk block (unless the policy forbids it).",
    ),
) -> None:
    """Add a rule from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "rule", assume_yes=yes)
    risk_mod.prewarm(_here(project))  # overlap model load with the clone below
    with _scanning(f"Scanning {qualified_name}…"):
        installed = rule_install_mod.install(
            _here(project), qualified_name, pin=pin, track=track, override_risk=override_risk
        )
    typer.echo(f"added rule {qualified_name} {installed.current.identifier()}")


@app.command("update")
@_friendly
def rule_update(
    qualified_name: str | None = typer.Argument(
        None, help="<repo_alias>/<rule_name>; omit and use --all/--repo for bulk."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    all_rules: bool = typer.Option(False, "--all", help="Update every installed rule."),
    repo: str | None = typer.Option(None, "--repo", help="Update only this repo's rules."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip rules already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Update despite a risk block (unless the policy forbids it)."
    ),
) -> None:
    """Refresh an installed rule, or update in bulk with --all / --repo."""
    if qualified_name is not None:
        updated = rule_install_mod.update(
            _here(project), qualified_name, force=force, override_risk=override_risk
        )
        typer.echo(f"updated rule {qualified_name} -> {updated.current.identifier()}")
        return
    if not all_rules and repo is None:
        raise typer.BadParameter("pass a <name>, --all, or --repo <alias>")
    _run_bulk_update(
        rule_install_mod.update_many, project, repo, only_outdated, force, override_risk
    )


@app.command("remove")
@_friendly
def rule_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<rule_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed rule from the project."""
    rule_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed rule {qualified_name}")


@app.command("rollback")
@_friendly
def rule_rollback(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<rule_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a rule."""
    rolled = rule_install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back rule {qualified_name} -> {rolled.current.identifier()}")
