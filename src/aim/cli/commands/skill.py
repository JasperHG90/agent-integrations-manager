"""`aim skill`: discover and manage skills."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_format, _here, _qualified_for_add, _scanning
from aim.core import format as format_mod
from aim.core import install as install_mod
from aim.core import risk as risk_mod
from aim.core import skills as skills_mod

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Discover and manage skills.")


@app.command("list")
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


@app.command("search")
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


@app.command("view")
@_friendly
def skill_view(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<skill_name> to display."),
) -> None:
    """Print an indexed skill's SKILL.md source."""
    typer.echo(skills_mod.read_skill_content(qualified_name))


@app.command("add")
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
    override_risk: bool = typer.Option(
        False,
        "--override-risk",
        help="Install despite a risk block (unless the policy forbids it).",
    ),
) -> None:
    """Add a skill from a git repository, registering the repo if needed."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "skill", assume_yes=yes)
    risk_mod.prewarm(_here(project))  # overlap model load with the clone below
    with _scanning(f"Scanning {qualified_name}…"):
        installed = install_mod.install(
            _here(project), qualified_name, pin=pin, track=track, override_risk=override_risk
        )
    typer.echo(f"added {qualified_name} {installed.current.identifier()} -> {installed.target_dir}")
    for warn in install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


@app.command("install", hidden=True)
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
    override_risk: bool = typer.Option(
        False,
        "--override-risk",
        help="Install despite a risk block (unless the policy forbids it).",
    ),
) -> None:
    """Deprecated: install an already-registered skill by qualified name. Use `add`."""
    installed = install_mod.install(
        _here(project), qualified_name, pin=pin, track=track, override_risk=override_risk
    )
    typer.echo(
        f"installed {qualified_name} {installed.current.identifier()} -> {installed.target_dir}"
    )
    for warn in install_mod.take_install_warnings():
        typer.echo(f"  warning: {warn}", err=True)
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


@app.command("update")
@_friendly
def skill_update(
    qualified_name: str | None = typer.Argument(
        None, help="<repo_alias>/<skill_name>; omit and use --all/--repo for bulk."
    ),
    project: Path | None = typer.Argument(None),
    all_skills: bool = typer.Option(False, "--all", help="Update every installed skill."),
    repo: str | None = typer.Option(None, "--repo", help="Update only this repo's skills."),
    only_outdated: bool = typer.Option(False, "--outdated", help="Skip skills already at HEAD."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
    diff: bool = typer.Option(False, "--diff", help="Show proposed change(s); don't apply."),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Update despite a risk block (unless the policy forbids it)."
    ),
) -> None:
    """Refresh an installed skill, or update in bulk with --all / --repo."""
    if qualified_name is not None:
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
        updated = install_mod.update(
            _here(project), qualified_name, force=force, override_risk=override_risk
        )
        assert not isinstance(updated, install_mod.UpdatePreview)
        typer.echo(f"updated {qualified_name} -> {updated.current.identifier()}")
        return
    if not all_skills and repo is None:
        raise typer.BadParameter("pass a <name>, --all, or --repo <alias>")
    outcomes = install_mod.update_many(
        _here(project),
        repo_alias=repo,
        only_outdated=only_outdated,
        force=force,
        dry_run=diff,
        override_risk=override_risk,
    )
    for outcome in outcomes:
        typer.echo(f"{outcome.status:>12}  {outcome.qualified_name}  {outcome.detail}")
    if any(outcome.status == "error" for outcome in outcomes):
        raise typer.Exit(code=1)


@app.command("remove")
@_friendly
def skill_remove(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<skill_name>"),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Remove an installed skill from the project."""
    install_mod.delete(_here(project), qualified_name)
    typer.echo(f"removed {qualified_name}")


@app.command("uninstall", hidden=True)
@_friendly
def skill_uninstall(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    skill_remove(qualified_name, project)


@app.command("delete", hidden=True)
@_friendly
def skill_delete(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
) -> None:
    """Deprecated alias for "remove"."""
    skill_remove(qualified_name, project)


@app.command("rollback")
@_friendly
def skill_rollback(
    qualified_name: str = typer.Argument(...),
    project: Path | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Restore the previous installed version of a skill."""
    rolled = install_mod.rollback(_here(project), qualified_name, force=force)
    typer.echo(f"rolled back {qualified_name} -> {rolled.current.identifier()}")
