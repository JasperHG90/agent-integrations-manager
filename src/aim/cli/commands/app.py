"""`aim app`: manage the aim installation — project schema, global cache, user config."""

from __future__ import annotations

from pathlib import Path

import typer

from aim.cli._shared import _friendly, _get_format, _here

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Manage the aim installation: project schema and the global cache database.",
)

config_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read and edit the per-user aim config (TUI preferences).",
)
app.add_typer(config_app, name="config")


@app.callback()
def app_group() -> None:
    """Keep `app` a command group: a single-command Typer would otherwise be collapsed
    into its lone command when materialized standalone by the lazy loader."""


@app.command("bump-manifest")
@_friendly
def bump_manifest(project: Path | None = typer.Argument(None, help="Project root.")) -> None:
    """Migrate `aim.toml` to the current schema version and rewrite it.

    Brings an older `manifest_version` up to date and materializes any newly-defaulted
    tables (such as the always-present `[archetype]` block). Re-running on an
    already-current file rewrites it idempotently.
    """
    from aim.core import declarations

    root = _here(project)
    from_version, to_version = declarations.bump(root)
    if from_version == to_version:
        typer.echo(f"aim.toml already at schema version {to_version}.")
    else:
        typer.echo(f"Bumped aim.toml schema version {from_version} -> {to_version}.")


@app.command("unlock-db")
@_friendly
def unlock_db() -> None:
    """Recover the global cache database after a "database is locked" failure.

    Checkpoints the write-ahead log to release WAL state. If another aim or TUI
    process is actively holding the database, this reports that instead — close that
    process and retry.
    """
    from aim.core import db as db_mod

    for action in db_mod.unlock():
        typer.echo(action)


@config_app.command("list")
@_friendly
def config_list(ctx: typer.Context) -> None:
    """List every user-config key with its current and default value."""
    from aim.core import format as format_mod
    from aim.core import user_config

    rows = [
        {"key": key, "value": value, "default": default}
        for key, value, default in user_config.list_values()
    ]
    format_mod.render(
        rows,
        _get_format(ctx),
        title="user config",
        columns=["key", "value", "default"],
        compact_columns=["key", "value"],
    )


@config_app.command("get")
@_friendly
def config_get(key: str = typer.Argument(..., help="Dotted config key.")) -> None:
    """Print the value of one user-config key."""
    from aim.core import user_config

    try:
        typer.echo(user_config.get(key))
    except user_config.UnknownConfigKeyError as exc:
        raise typer.BadParameter(f"unknown config key: {exc.args[0]}") from exc


@config_app.command("set")
@_friendly
def config_set(
    key: str = typer.Argument(..., help="Dotted config key."),
    value: str = typer.Argument(..., help="New value (booleans: true/false)."),
) -> None:
    """Set a user-config key and persist it."""
    from aim.core import user_config

    try:
        user_config.set_value(key, value)
    except user_config.UnknownConfigKeyError as exc:
        raise typer.BadParameter(f"unknown config key: {exc.args[0]}") from exc
    except user_config.InvalidConfigValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"set {key} = {user_config.get(key)}")


@config_app.command("unset")
@_friendly
def config_unset(key: str = typer.Argument(..., help="Dotted config key.")) -> None:
    """Reset a user-config key to its default."""
    from aim.core import user_config

    try:
        user_config.unset(key)
    except user_config.UnknownConfigKeyError as exc:
        raise typer.BadParameter(f"unknown config key: {exc.args[0]}") from exc
    typer.echo(f"reset {key} to {user_config.get(key)}")


@config_app.command("path")
@_friendly
def config_path_cmd() -> None:
    """Print the location of the user config file."""
    from aim.core import user_config

    typer.echo(str(user_config.config_path()))
