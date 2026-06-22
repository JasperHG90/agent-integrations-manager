"""`aim repo`: manage skill/agent/rule source repositories."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_allow_insecure, _get_format, _here
from aim.core import format as format_mod
from aim.core import repos as repos_mod

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Manage skill/agent/rule source repositories."
)


@app.command("add")
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


@app.command("list")
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


@app.command("remove")
@_friendly
def repo_remove(
    alias: str,
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Unregister a source repo and delete its local clone.

    This is a global cache eviction and does not touch any project's aim.toml — a
    project that still declares artifacts from this repo keeps them, and `sync`
    re-registers the repo from the lockfile when needed.
    """
    declared = repos_mod.project_artifacts_for_repo(_here(project), alias)
    repos_mod.remove(alias)
    typer.echo(f"removed repo {alias}")
    if declared:
        typer.echo(
            f"  note: this project still declares {len(declared)} artifact(s) from "
            f"{alias}; remove them with `aim skill/agent/rule remove <name>`:",
            err=True,
        )
        for qualified_name in declared:
            typer.echo(f"    {qualified_name}", err=True)


@app.command("rename")
@_friendly
def repo_rename(old: str, new: str) -> None:
    """Rename a registered repo alias (moves its clone and index rows)."""
    repos_mod.rename(old, new)
    typer.echo(f"renamed {old} -> {new}")


@app.command("refresh")
@_friendly
def repo_refresh(
    ctx: typer.Context,
    alias: str | None = typer.Argument(
        None, help="Repo alias to refresh. Omit to refresh every registered repo."
    ),
) -> None:
    """Fetch the latest commits for a registered repo (or all repos) and re-index."""
    allow_insecure = _get_allow_insecure(ctx)
    if alias is not None:
        repo = repos_mod.refresh(alias, allow_insecure=allow_insecure)
        sha = repo.last_sha[:12] if repo.last_sha else "?"
        typer.echo(f"refreshed {alias}: HEAD={sha}")
        return
    aliases = [r.alias for r in repos_mod.list_repos()]
    if not aliases:
        typer.echo("no repos registered")
        return
    failures = 0
    for a, refreshed, err in repos_mod.refresh_many(aliases, allow_insecure=allow_insecure):
        if err is not None:
            failures += 1
            typer.echo(f"  {a}: {err}", err=True)
            continue
        sha = refreshed.last_sha[:12] if refreshed and refreshed.last_sha else "?"
        typer.echo(f"refreshed {a}: HEAD={sha}")
    if failures:
        raise typer.Exit(code=1)
