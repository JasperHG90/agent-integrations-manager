"""`aim archetype`: discover, select, and update project-instruction archetypes."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import (
    _friendly,
    _get_allow_insecure,
    _get_format,
    _here,
    _looks_like_url,
    _parse_source_subpath,
    _parse_source_url,
    _qualified_for_add,
    _resolve_or_register_repo,
    _scanning,
)
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
                "via": "-",
                "title": "Built-in template",
                "description": "aim's bundled AGENTS.md scaffold (no archetype)",
            }
        )
    display += [
        {
            "qualified_name": r.qualified_name,
            "repo_alias": r.repo_alias,
            "available": r.available,
            "via": r.indexed_via or archetypes_mod.VIA_DISCOVERED,
            "title": r.title or "",
            "description": r.description or "",
        }
        for r in archetypes_mod.list_archetypes(repo)
    ]
    format_mod.render(
        display,
        _get_format(ctx),
        title="archetypes indexed",
        columns=["qualified_name", "repo_alias", "available", "via", "title", "description"],
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
        help=(
            "Web link to an instruction file or directory (tree/blob URL), a git "
            "clone URL, '<alias>/<name>' of an indexed archetype, or a registered "
            "repo alias combined with --path."
        ),
    ),
    name: str | None = typer.Argument(
        None, help="Archetype name within the repo (inferred from the URL/path if omitted)."
    ),
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    alias: str | None = typer.Option(
        None, "--alias", help="Repo alias to register under (default: derived from the URL)."
    ),
    path: str | None = typer.Option(
        None,
        "--path",
        help=(
            "Repo-relative path to an instruction file (e.g. AGENTS.md, docs/CLAUDE.md) "
            "or a directory holding one; registers it as an explicit archetype link."
        ),
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
    """Select an instruction archetype as this project's AGENTS.md base.

    Only canonical `instructions/<name>/` directories are auto-discovered; any
    other AGENTS.md / CLAUDE.md / GEMINI.md in a repo counts only when linked
    explicitly — by passing a web URL that points at the file (or its
    directory), or a registered repo alias plus --path. The link is recorded in
    aim.toml / aim.lock.toml, so teammates retrieve it on `aim sync`.
    """
    qualified_name = _resolve_archetype_target(ctx, url, name, alias, path, assume_yes=yes)
    root = _here(project)
    risk_mod.prewarm(root)
    with _scanning(f"Scanning {qualified_name}…"):
        installed = archetype_install_mod.select(
            root, qualified_name, pin=pin, track=track, override_risk=override_risk
        )
    typer.echo(f"using instruction archetype {qualified_name} {installed.current.identifier()}")
    for warn in archetype_install_mod.take_render_warnings():
        typer.echo(f"  warning: {warn}", err=True)
    for warn in risk_mod.take_risk_warnings():
        typer.echo(f"  risk: {warn}", err=True)


def _resolve_archetype_target(
    ctx: typer.Context,
    url: str,
    name: str | None,
    alias: str | None,
    path: str | None,
    *,
    assume_yes: bool,
) -> str:
    """Resolve `archetype use`'s target to a qualified name, linking when explicit.

    A web tree/blob URL (which carries an in-repo path) or an explicit --path
    registers the target as an archetype link — unless the path is a canonical
    `instructions/<name>` location, which is auto-discovered and routed through
    the discovered flow (a link row there would only shadow the discovered one
    and outlive it). A plain clone URL or a bare '<alias>/<name>' always takes
    the discovered flow.
    """
    subpath = _parse_source_subpath(url) if _looks_like_url(url) else None
    if path is not None and subpath is not None:
        raise typer.BadParameter(
            "--path conflicts with a URL that already points into the repo; pass one or the other"
        )
    link_path = path or subpath
    if link_path is None:
        return _qualified_for_add(ctx, url, name, alias, "archetype", assume_yes=assume_yes)
    canonical = archetypes_mod.canonical_archetype_name(link_path)
    if _looks_like_url(url):
        if canonical is not None:
            qualified_name = _qualified_for_add(
                ctx, url, name or canonical, alias, "archetype", assume_yes=assume_yes
            )
            _note_canonical_base(qualified_name, link_path)
            return qualified_name
        clone_url, ref, _ = _parse_source_url(url)
        repo_alias = _resolve_or_register_repo(
            clone_url,
            alias=alias,
            allow_insecure=_get_allow_insecure(ctx),
            default_ref=ref,
            assume_yes=assume_yes,
        )
    else:
        # --path with a bare target: it must name an already-registered repo alias.
        from aim.core import repos as repos_mod

        if "/" in url or url not in {r.alias for r in repos_mod.list_repos()}:
            raise typer.BadParameter(
                f"--path needs a git URL or a registered repo alias, got {url!r}"
            )
        repo_alias = url
        if canonical is not None:
            qualified_name = f"{repo_alias}/{name or canonical}"
            _note_canonical_base(qualified_name, link_path)
            return qualified_name
    row = archetypes_mod.register_link(repo_alias, link_path, name=name)
    return row.qualified_name


def _note_canonical_base(qualified_name: str, link_path: str) -> None:
    """Warn when a canonical path named a file other than the discovered base.

    Canonical archetypes choose their base by priority (AGENTS.md first), so a
    URL/--path naming e.g. CLAUDE.md still selects the priority base — say so
    instead of silently substituting a different file.
    """
    base = link_path.strip().strip("/").rsplit("/", 1)[-1]
    if not archetypes_mod.is_instruction_filename(base):
        return  # a directory path names no specific file
    try:
        row = archetypes_mod.index_row(qualified_name)
    except archetypes_mod.ArchetypeNotIndexedError:
        return
    if row.instruction_path != link_path.strip().strip("/"):
        typer.echo(
            f"note: {qualified_name} is a canonical archetype; using its discovered base "
            f"{row.instruction_path} (the base file is chosen by priority, not by the link)",
            err=True,
        )


@app.command("unlink")
@_friendly
def archetype_unlink(
    qualified_name: str = typer.Argument(
        ..., help="<repo_alias>/<name> of an explicitly linked archetype."
    ),
) -> None:
    """Remove an explicitly linked archetype from the index.

    Only removes the browse/select index entry; a project that selected it
    keeps its pin in aim.toml / aim.lock.toml and keeps rendering.
    """
    archetypes_mod.unregister_link(qualified_name)
    typer.echo(f"unlinked archetype {qualified_name}")
    typer.echo(
        "  note: a project whose lockfile still selects it re-registers the link "
        "on its next `aim sync`",
        err=True,
    )


@app.command("update")
@_friendly
def archetype_update(
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
    override_risk: bool = typer.Option(
        False, "--override-risk", help="Update despite a risk block (unless policy forbids it)."
    ),
) -> None:
    """Re-resolve the selected archetype to its tracked ref and re-render AGENTS.md."""
    with _scanning("Updating instructions…"):
        updated = archetype_install_mod.update(_here(project), override_risk=override_risk)
    typer.echo(
        f"updated instruction archetype {updated.qualified_name} -> {updated.current.identifier()}"
    )
    for warn in archetype_install_mod.take_render_warnings():
        typer.echo(f"  warning: {warn}", err=True)


@app.command("clear")
@_friendly
def archetype_clear(
    project: Path | None = typer.Argument(None, help="Project root (defaults to cwd)."),
) -> None:
    """Clear the selected archetype, reverting AGENTS.md to the built-in template."""
    archetype_install_mod.clear(_here(project))
    typer.echo("cleared instruction archetype; using the built-in template")
    for warn in archetype_install_mod.take_render_warnings():
        typer.echo(f"  warning: {warn}", err=True)
