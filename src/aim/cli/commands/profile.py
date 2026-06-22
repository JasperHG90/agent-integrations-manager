"""`aim template`: manage, share, apply, and update reusable project templates."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_allow_insecure, _get_format, _here
from aim.core import format as format_mod
from aim.core import profiles as profiles_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Save, share, apply, and update reusable project templates.",
)


@app.command("save")
@_friendly
def profile_save(
    name: str,
    project: Path | None = typer.Argument(None, help="Project to snapshot as a reusable template."),
) -> None:
    """Snapshot a project's declarations into a reusable named template."""
    profile = profiles_mod.from_project(name, _here(project))
    path = profiles_mod.save(profile)
    typer.echo(f"saved project template {name} to {path}")


@app.command("list")
@_friendly
def profile_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(
        None, "--repo", help="List project templates discovered in a registered repo."
    ),
) -> None:
    """List saved project templates, or templates discovered in a repo with --repo."""
    if repo is not None:
        from aim.core import repo_templates as repo_templates_mod

        template_rows = [
            {
                "qualified_name": t.qualified_name,
                "name": t.template_name,
                "description": t.description or "-",
            }
            for t in repo_templates_mod.list_templates(repo)
        ]
        format_mod.render(
            template_rows,
            _get_format(ctx),
            title=f"templates in {repo}",
            columns=["qualified_name", "name", "description"],
            compact_columns=["qualified_name", "description"],
        )
        return
    entries = profiles_mod.list_profiles()
    rows = [
        {
            "name": p.name,
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
        title="templates saved",
        columns=["name", "symlinks", "skills", "subagents", "mcp", "rules"],
        compact_columns=["name", "skills", "subagents", "mcp", "rules"],
    )


@app.command("show")
@_friendly
def profile_show(name: str) -> None:
    """Print a saved template as JSON."""
    p = profiles_mod.load(name)
    typer.echo(p.model_dump_json(indent=2))


@app.command("delete")
@_friendly
def profile_delete(name: str) -> None:
    """Delete a saved template."""
    removed = profiles_mod.delete(name)
    typer.echo(f"deleted {name}" if removed else f"not found: {name}")


@app.command("export")
@_friendly
def profile_export(
    name: str,
    path: Path | None = typer.Argument(
        None, help="Output .toml path (default: <name>.toml; '-' for stdout)."
    ),
) -> None:
    """Export a saved template to a shareable TOML file."""
    profile = profiles_mod.load(name)
    text = profiles_mod.render_toml(profile)
    if path is not None and str(path) == "-":
        typer.echo(text)
        return
    out = path or Path(f"{name}.toml")
    out.write_text(text, encoding="utf-8")
    typer.echo(f"exported project template {name} to {out}")


@app.command("import")
@_friendly
def profile_import(
    path: Path,
    name: str | None = typer.Option(None, "--name", help="Override the imported template's name."),
) -> None:
    """Import a project template from a TOML file into your saved templates."""
    profile = profiles_mod.parse_toml(path.read_text(encoding="utf-8"), source=str(path))
    if name is not None:
        profile = profile.model_copy(update={"name": name})
    saved_path = profiles_mod.save(profile)
    typer.echo(f"imported project template {profile.name} to {saved_path}")


@app.command("apply")
@_friendly
def profile_apply(
    ctx: typer.Context,
    name: str,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Apply a saved or repo template to a project: init, lock, install artifacts, sync.

    Source repos the template needs are cloned automatically from the urls it
    records (each screened against the project's policy).
    """
    result = profiles_mod.apply(
        name,
        _here(project),
        allow_insecure=_get_allow_insecure(ctx),
    )
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


@app.command("check")
@_friendly
def profile_check(
    project: Path | None = typer.Argument(None, help="Project root."),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Check whether the applied template has drifted from its upstream version.

    Exit codes: 0 = up to date (or not stamped from a template); 2 = the upstream
    template changed since it was applied.
    """
    result = profiles_mod.check(_here(project))
    if json_out:
        payload = {
            "has_template": result.has_template,
            "template": result.qualified_name,
            "locked_hash": result.locked_hash,
            "upstream_hash": result.upstream_hash,
            "drift": result.drift,
            "up_to_date": result.up_to_date,
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        if not result.has_template:
            typer.echo("project was not stamped from a shared template")
            return
        typer.echo(f"template: {result.qualified_name}")
        if result.drift:
            typer.echo(
                f"  DRIFT: upstream template changed "
                f"(applied {(result.locked_hash or '-')[:12]}, "
                f"upstream {(result.upstream_hash or '-')[:12]})",
                err=True,
            )
        else:
            typer.echo("up to date")

    if not result.has_template or result.up_to_date:
        return
    raise typer.Exit(code=2)


@app.command("diff")
@_friendly
def profile_diff(project: Path | None = typer.Argument(None, help="Project root.")) -> None:
    """Preview which template-owned artifacts an update would add or remove."""
    d = profiles_mod.diff(_here(project))
    typer.echo(f"template: {d.qualified_name}")
    for member in d.added:
        typer.echo(f"  + {member}")
    for member in d.removed:
        typer.echo(f"  - {member}")
    if not d.added and not d.removed:
        typer.echo("  (no structural changes)")


@app.command("bump")
@_friendly
def profile_bump(
    ctx: typer.Context,
    name: str,
    artifact: str | None = typer.Argument(
        None, help="Single <alias>/<name> to bump; omit to bump every artifact."
    ),
) -> None:
    """Advance a saved template's pinned artifact SHAs to the latest from their repos."""
    changes = profiles_mod.bump(name, only=artifact, allow_insecure=_get_allow_insecure(ctx))
    if not changes:
        typer.echo(f"template {name} is already up to date")
        return
    for change in changes:
        old = (change.old_sha or "unpinned")[:12]
        typer.echo(f"  {change.qualified_name}: {old} -> {change.new_sha[:12]}")
    typer.echo(f"bumped {len(changes)} artifact(s) in template {name}")


@app.command("update")
@_friendly
def profile_update(
    ctx: typer.Context,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Converge the project to the latest version of its recorded template."""
    result = profiles_mod.update_from_template(
        _here(project),
        allow_insecure=_get_allow_insecure(ctx),
    )
    for member in result.removed:
        typer.echo(f"  removed: {member}")
    for qn in result.apply_result.installed_skills:
        typer.echo(f"  skill: {qn}")
    for qn in result.apply_result.installed_agents:
        typer.echo(f"  agent: {qn}")
    for qn in result.apply_result.installed_rules:
        typer.echo(f"  rule: {qn}")
    typer.echo(f"updated to template {result.apply_result.project_root}")
