"""`aim repo`: manage skill/agent/rule source repositories."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import (
    _friendly,
    _get_allow_insecure,
    _get_format,
    _here,
    _tracked_ref_lag_cell,
    _warn_tracked_ref_lag,
)
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
    _warn_skipped_templates()


@app.command("list")
@_friendly
def repo_list(ctx: typer.Context) -> None:
    """List registered skill source repositories."""
    # Decorate each repo with a computed `behind` cell (a per-repo git call), so the
    # table shows when a tracked ref trails the remote default branch. Keep the full
    # model fields (via model_dump) so `--json` output stays a superset of before.
    rows = [
        {**r.model_dump(mode="json"), "behind": _tracked_ref_lag_cell(r.alias)}
        for r in repos_mod.list_repos()
    ]
    format_mod.render(
        rows,
        _get_format(ctx),
        title="repos registered",
        columns=["alias", "url", "default_ref", "behind", "head", "last_fetched"],
        row_extractor={"head": "last_sha", "last_fetched": "last_fetched_at"},
        compact_columns=["alias", "url", "default_ref", "behind"],
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


@app.command("set-ref")
@_friendly
def repo_set_ref(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Registered repo alias."),
    ref: str = typer.Argument(..., help="Branch or tag to track (e.g. main, HEAD, v1.2.0)."),
) -> None:
    """Change which branch/tag a registered repo tracks, then re-resolve and reindex."""
    repo = repos_mod.set_ref(alias, ref, allow_insecure=_get_allow_insecure(ctx))
    sha = repo.last_sha[:12] if repo.last_sha else "?"
    typer.echo(f"set {alias} ref -> {ref} (HEAD={sha})")
    _warn_tracked_ref_lag(alias)  # normally silent now; warns if the new ref is still behind
    _warn_skipped_templates()


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
        _warn_tracked_ref_lag(alias)
        _warn_skipped_templates()
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
        _warn_tracked_ref_lag(a)
    _warn_skipped_templates()
    if failures:
        raise typer.Exit(code=1)


@app.command("reindex")
@_friendly
def repo_reindex(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Repo alias to reindex."),
) -> None:
    """Fetch a registered repo and re-run artifact discovery unconditionally.

    Like `refresh`, but reindexes even when the tracked commit is unchanged — use it
    to pick up artifacts (e.g. plugins newly discoverable under a custom kind) that a
    plain `refresh` would skip because the SHA did not move.
    """
    allow_insecure = _get_allow_insecure(ctx)
    repo = repos_mod.reindex(alias, allow_insecure=allow_insecure)
    sha = repo.last_sha[:12] if repo.last_sha else "?"
    typer.echo(f"reindexed {alias}: HEAD={sha}")
    _warn_skipped_templates()


def _warn_skipped_templates() -> None:
    """Print a stderr warning for any template skipped (unparseable) during indexing."""
    from aim.core import repo_templates as repo_templates_mod

    for warning in repo_templates_mod.take_skipped_warnings():
        typer.echo(f"warning: {warning}", err=True)
