"""`aim plugin`: discover and manage plugins from indexed marketplaces."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_format, _here, _scanning
from aim.core import format as format_mod
from aim.core import plugin_install as plugin_install_mod
from aim.core import plugins as plugins_mod
from aim.core import risk as risk_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Discover and manage plugins from indexed marketplaces.",
)


def _resolve_qualified(name: str, repo: str | None) -> str:
    """Resolve a plugin name (or ``<repo>/<name>``) to an indexed qualified name.

    Lets the user add a plugin without naming its marketplace: a bare name that
    is unique across indexed marketplaces resolves directly; an ambiguous one
    must be qualified as ``<repo>/<name>``.

    Raises:
        PluginNotIndexedError: No indexed plugin matches (add its repo first).
        typer.BadParameter: The bare name is ambiguous across repos.
    """
    # A fully-qualified name is unambiguous; resolve it ignoring the --repo filter.
    if "/" in name:
        for row in plugins_mod.list_plugins():
            if row.qualified_name == name:
                return name
    rows = plugins_mod.list_plugins(repo_alias=repo)
    matches = [r for r in rows if r.plugin_name == name or r.qualified_name == name]
    if len(matches) == 1:
        return matches[0].qualified_name
    if not matches:
        raise plugins_mod.PluginNotIndexedError(
            f"{name!r} not found in any indexed marketplace; add its repo with "
            "`aim repo add <url>` first, then `aim plugin list`"
        )
    options = ", ".join(sorted(m.qualified_name for m in matches))
    raise typer.BadParameter(f"{name!r} is ambiguous; qualify as <repo>/<name>: {options}")


@app.command("list")
@_friendly
def plugin_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
    marketplace: str | None = typer.Option(
        None, "--marketplace", "-m", help="Filter by marketplace name."
    ),
    flavor: str | None = typer.Option(
        None, "--flavor", help="Filter by flavor: 'claude' or 'opencode'."
    ),
) -> None:
    """List indexed plugins across all marketplaces."""
    rows = plugins_mod.list_plugins(repo_alias=repo, marketplace=marketplace, flavor=flavor)
    format_mod.render(
        rows,
        _get_format(ctx),
        title="plugins indexed",
        columns=["qualified_name", "flavor", "marketplace_name", "version", "sha", "description"],
        compact_columns=["qualified_name", "flavor", "sha", "description"],
        row_extractor={"sha": "short_sha"},
    )


@app.command("search")
@_friendly
def plugin_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Substring to match."),
) -> None:
    """Search indexed plugins by substring."""
    rows = plugins_mod.search(query)
    format_mod.render(
        rows,
        _get_format(ctx),
        title=f"plugins matching {query!r}",
        columns=["qualified_name", "flavor", "marketplace_name", "version", "sha", "description"],
        compact_columns=["qualified_name", "flavor", "sha", "description"],
        row_extractor={"sha": "short_sha"},
    )


@app.command("marketplaces")
@_friendly
def plugin_marketplaces(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    """List indexed plugin marketplaces."""
    rows = plugins_mod.list_marketplaces(repo_alias=repo)
    format_mod.render(
        rows,
        _get_format(ctx),
        title="marketplaces indexed",
        columns=["qualified_name", "repo_alias", "owner_name", "description"],
        compact_columns=["qualified_name", "description"],
    )


@app.command("view")
@_friendly
def plugin_view(
    name: str = typer.Argument(..., help="Plugin name or <repo_alias>/<plugin_name> to display."),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Disambiguate by repo alias."),
) -> None:
    """Print an indexed plugin's manifest (plugin.json, or the file for opencode)."""
    qualified_name = _resolve_qualified(name, repo)
    typer.echo(plugins_mod.read_plugin_content(qualified_name))


@app.command("add")
@_friendly
def plugin_add(
    name: str = typer.Argument(
        ..., help="Plugin name (or <repo_alias>/<plugin_name>) from an indexed marketplace."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    repo: str | None = typer.Option(
        None, "--repo", "-r", help="Disambiguate a bare name by repo alias."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None, "--track", help="Ref to track on update (branch, tag, or 'latest-tag')."
    ),
    override_risk: bool = typer.Option(
        False,
        "--override-risk",
        help="Install despite a risk block (unless the policy forbids it).",
    ),
) -> None:
    """Vendor a plugin into the project and register it with the client."""
    qualified_name = _resolve_qualified(name, repo)
    risk_mod.prewarm(_here(project))
    with _scanning(f"Scanning {qualified_name}…"):
        installed = plugin_install_mod.install_plugin(
            _here(project), qualified_name, pin=pin, track=track, override_risk=override_risk
        )
    typer.echo(f"added {qualified_name} {installed.current.identifier()} -> {installed.target_dir}")
    for warn in plugin_install_mod.take_install_warnings():
        typer.echo(f"  review: {warn}", err=True)
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


@app.command("update")
@_friendly
def plugin_update(
    name: str | None = typer.Argument(
        None, help="Plugin name; omit and use --all/--repo for bulk."
    ),
    project: Path | None = typer.Argument(None),
    all_plugins: bool = typer.Option(False, "--all", help="Update every installed plugin."),
    repo: str | None = typer.Option(None, "--repo", help="Update only this repo's plugins."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Update despite a risk block (unless the policy forbids it)."
    ),
) -> None:
    """Update an installed plugin, or update in bulk with --all / --repo."""
    if name is not None:
        qualified_name = _resolve_qualified(name, repo)
        updated = plugin_install_mod.update(
            _here(project), qualified_name, force=force, override_risk=override_risk
        )
        typer.echo(f"updated {qualified_name} -> {updated.current.identifier()}")
        return
    if not all_plugins and repo is None:
        raise typer.BadParameter("pass a <name>, --all, or --repo <alias>")
    outcomes = plugin_install_mod.update_many(
        _here(project), repo_alias=repo, force=force, override_risk=override_risk
    )
    for outcome in outcomes:
        typer.echo(f"{outcome.status:>12}  {outcome.qualified_name}  {outcome.detail}")
    if any(outcome.status == "error" for outcome in outcomes):
        raise typer.Exit(code=1)


@app.command("remove")
@_friendly
def plugin_remove(
    name: str = typer.Argument(..., help="Plugin name or <repo_alias>/<plugin_name>."),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Disambiguate by repo alias."),
) -> None:
    """Remove an installed plugin from the project."""
    qualified_name = _resolve_installed(_here(project), name, repo)
    plugin_install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed {qualified_name}")


@app.command("rollback")
@_friendly
def plugin_rollback(
    name: str = typer.Argument(..., help="Plugin name or <repo_alias>/<plugin_name>."),
    project: Path | None = typer.Argument(None),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Disambiguate by repo alias."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a plugin."""
    qualified_name = _resolve_installed(_here(project), name, repo)
    rolled = plugin_install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back {qualified_name} -> {rolled.current.identifier()}")


def _resolve_installed(project_root: Path, name: str, repo: str | None) -> str:
    """Resolve a plugin name against the project's INSTALLED plugins.

    Removal/rollback operate on the manifest (not the index), so a plugin whose
    repo is no longer indexed can still be removed.

    Raises:
        PluginNotInstalledError: No installed plugin matches.
        typer.BadParameter: The bare name is ambiguous across repos.
    """
    from aim.core import manifest as manifest_mod

    try:
        m = manifest_mod.load(project_root)
    except manifest_mod.ManifestNotFoundError:
        raise plugin_install_mod.PluginNotInstalledError(name) from None
    # A fully-qualified name is unambiguous; resolve it ignoring the --repo filter.
    if "/" in name:
        for p in m.plugins:
            if p.qualified_name == name:
                return name
    installed = m.plugins
    if repo is not None:
        installed = [p for p in installed if p.repo_alias == repo]
    matches = [
        p
        for p in installed
        if p.qualified_name.split("/", 1)[1] == name or p.qualified_name == name
    ]
    if len(matches) == 1:
        return matches[0].qualified_name
    if not matches:
        raise plugin_install_mod.PluginNotInstalledError(name)
    options = ", ".join(sorted(p.qualified_name for p in matches))
    raise typer.BadParameter(f"{name!r} is ambiguous; qualify as <repo>/<name>: {options}")
