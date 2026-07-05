"""`aim mcp`: manage .mcp.json entries via the public registry."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_allow_insecure, _get_format, _here, _scanning
from aim.core import format as format_mod
from aim.core import manifest as manifest_mod
from aim.core import mcp_install as mcp_install_mod
from aim.core import mcp_registry as mcp_registry_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="MCP servers: manage .mcp.json entries via the public registry.",
)


def _parse_key_value_list(items: list[str]) -> dict[str, str]:
    """Parse `NAME=VALUE` strings into a dict.

    Raises:
        typer.BadParameter: An item is missing the `=` separator.
    """
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"expected NAME=VALUE, got {item!r}")
        name, _, value = item.partition("=")
        out[name] = value
    return out


def _parse_header_list(items: list[str]) -> dict[str, str]:
    """Parse `Name:Value` header strings into a dict, stripping whitespace.

    Raises:
        typer.BadParameter: An item is missing the `:` separator.
    """
    out: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            raise typer.BadParameter(f"expected Name:Value, got {item!r}")
        name, _, value = item.partition(":")
        out[name.strip()] = value.strip()
    return out


@app.command("search")
@_friendly
def mcp_search_cmd(query: str = typer.Argument(..., help="Search term.")) -> None:
    """Search the public MCP registry."""
    results, _ = mcp_registry_mod.search_registry(query)
    if not results:
        typer.echo(f"no MCP servers match {query!r}")
        return
    for r in results:
        server = r.server
        desc = f" — {server.description}" if server.description else ""
        typer.echo(f"{server.name}{desc}")


@app.command("list")
@_friendly
def mcp_list_cmd(
    ctx: typer.Context,
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """List MCP servers installed in the project."""
    m = manifest_mod.load_or_default(_here(project))
    format_mod.render(
        m.mcp_servers,
        _get_format(ctx),
        title="MCP servers installed",
        columns=["alias", "registry_name", "version"],
        row_extractor={
            "alias": "alias",
            "registry_name": "registry_name",
            "version": "current.registry_version",
        },
        compact_columns=["alias", "registry_name", "version"],
    )


@app.command("add")
@app.command("install", hidden=True)
@_friendly
def mcp_add_cmd(
    ctx: typer.Context,
    registry_name: str = typer.Argument(..., help="Canonical registry server name."),
    alias: str = typer.Argument(..., help="Local alias for .mcp.json -> mcpServers."),
    project: Path | None = typer.Argument(None, help="Project root."),
    transport: str | None = typer.Option(
        None, "--transport", help="Preferred transport: stdio, http, sse, ws."
    ),
    command: str | None = typer.Option(None, "--command", help="Override entry command."),
    arg: list[str] = typer.Option([], "--arg", help="Override entry args (repeatable)."),
    env: list[str] = typer.Option([], "--env", help="Override env var NAME=VALUE (repeatable)."),
    url: str | None = typer.Option(None, "--url", help="Override entry URL."),
    header: list[str] = typer.Option(
        [], "--header", help="Override header Name:Value (repeatable)."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing alias."),
) -> None:
    """Add an MCP server to the project's .mcp.json (by registry name)."""
    overrides: dict[str, object] = {}
    if command:
        overrides["command"] = command
    if arg:
        overrides["args"] = list(arg)
    if env:
        overrides["env"] = _parse_key_value_list(env)
    if url:
        overrides["url"] = url
    if header:
        overrides["headers"] = _parse_header_list(header)
    with _scanning(f"Installing {registry_name}…"):
        installed = mcp_install_mod.install(
            _here(project),
            registry_name,
            alias=alias,
            preferred_transport=transport,
            overrides=overrides or None,
            force=force,
            allow_insecure=_get_allow_insecure(ctx),
        )
    typer.echo(f"added MCP server {installed.registry_name} as {installed.alias}")


@app.command("update")
@_friendly
def mcp_update_cmd(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local edits."),
) -> None:
    """Refresh a managed MCP server from the registry."""
    with _scanning(f"Updating {alias}…"):
        updated = mcp_install_mod.update(
            _here(project), alias, force=force, allow_insecure=_get_allow_insecure(ctx)
        )
    typer.echo(f"updated MCP server {updated.alias} -> {updated.current.registry_version or '?'}")


@app.command("remove")
@app.command("uninstall", hidden=True)
@app.command("delete", hidden=True)
@_friendly
def mcp_remove_cmd(
    alias: str = typer.Argument(..., help="Local alias."),
    project: Path | None = typer.Argument(None, help="Project root."),
) -> None:
    """Remove a managed MCP server from .mcp.json."""
    mcp_install_mod.delete(_here(project), alias)
    typer.echo(f"removed MCP server {alias}")
