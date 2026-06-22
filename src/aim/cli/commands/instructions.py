"""`aim archetype`: discover, select, and update project-instruction archetypes."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_format, _here, _qualified_for_add, _scanning
from aim.core import archetype_install as archetype_install_mod
from aim.core import archetypes as archetypes_mod
from aim.core import format as format_mod
from aim.core import risk as risk_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Discover, select, update, and clear project-instruction archetypes.",
)


@app.command("list")
@_friendly
def archetype_list(
    ctx: typer.Context,
    repo: str | None = typer.Option(None, "--repo", "-r", help="Filter by repo alias."),
) -> None:
    """List indexed project-instruction archetypes (plus the built-in default)."""
    display: list[dict[str, str]] = []
    # The built-in default ships with aim and is always an option; it heads the
    # list unless a specific repo is requested.
    if repo is None:
        display.append(
            {
                "qualified_name": "default",
                "repo_alias": "-",
                "available": "-",
                "title": "Built-in template",
                "description": "aim's bundled AGENTS.md scaffold (no archetype)",
            }
        )
    display += [
        {
            "qualified_name": r.qualified_name,
            "repo_alias": r.repo_alias,
            "available": r.available,
            "title": r.title or "",
            "description": r.description or "",
        }
        for r in archetypes_mod.list_archetypes(repo)
    ]
    format_mod.render(
        display,
        _get_format(ctx),
        title="archetypes indexed",
        columns=["qualified_name", "repo_alias", "available", "title", "description"],
        compact_columns=["qualified_name", "available", "title"],
    )


@app.command("search")
@_friendly
def archetype_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Substring to match."),
) -> None:
    """Search indexed archetypes by substring."""
    rows = archetypes_mod.search(query)
    format_mod.render(
        rows,
        _get_format(ctx),
        title=f"archetypes matching {query!r}",
        columns=["qualified_name", "repo_alias", "available", "title", "description"],
        compact_columns=["qualified_name", "available", "title"],
    )


@app.command("view")
@_friendly
def archetype_view(
    qualified_name: str = typer.Argument(..., help="<repo_alias>/<archetype_name> to display."),
) -> None:
    """Print an indexed instruction archetype's base instruction body."""
    row = archetypes_mod.index_row(qualified_name)
    typer.echo(
        archetypes_mod.read_base_body(row.repo_alias, row.indexed_at_sha, row.instruction_path)
    )


@app.command("use")
@_friendly
def archetype_use(
    ctx: typer.Context,
    url: str = typer.Argument(
        ...,
        help="Git URL, or '<alias>/<name>' of an archetype in an already-registered repo.",
    ),
    name: str | None = typer.Argument(
        None, help="Archetype name within the repo (inferred from a tree URL if omitted)."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    alias: str | None = typer.Option(
        None, "--alias", help="Repo alias to register under (default: derived from the URL)."
    ),
    pin: str | None = typer.Option(
        None, "--pin", help="Pin to an exact tag/sha; update never advances past it."
    ),
    track: str | None = typer.Option(
        None, "--track", help="Ref to track on update (branch, tag, or 'latest-tag')."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Register the source repo without prompting."
    ),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Select despite a risk block (unless policy forbids it)."
    ),
) -> None:
    """Select an instruction archetype as this project's AGENTS.md base."""
    qualified_name = _qualified_for_add(ctx, url, name, alias, "archetype", assume_yes=yes)
    root = _here(project)
    risk_mod.prewarm(root)
    with _scanning(f"Scanning {qualified_name}…"):
        installed = archetype_install_mod.select(
            root, qualified_name, pin=pin, track=track, override_risk=override_risk
        )
    typer.echo(f"using instruction archetype {qualified_name} {installed.current.identifier()}")
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


@app.command("update")
@_friendly
def archetype_update(
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Update despite a risk block (unless policy forbids it)."
    ),
) -> None:
    """Re-resolve the selected archetype to its tracked ref and re-render AGENTS.md."""
    updated = archetype_install_mod.update(_here(project), override_risk=override_risk)
    typer.echo(
        f"updated instruction archetype {updated.qualified_name} -> {updated.current.identifier()}"
    )


@app.command("clear")
@_friendly
def archetype_clear(
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Clear the selected archetype, reverting AGENTS.md to the built-in template."""
    archetype_install_mod.clear(_here(project))
    typer.echo("cleared instruction archetype; using the built-in template")
