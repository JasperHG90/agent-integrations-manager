"""A Typer group that imports each subcommand's module only when it is dispatched.

Importing `aim.cli` happens on every invocation — including bare `aim` (which launches
the TUI) and `aim --version`. Registering each command group eagerly would import the
whole `aim.core` surface up front. `LazyTyperGroup` defers that: a group's module is
imported only when its command actually runs (or when `--help` enumerates groups).
"""

from __future__ import annotations

import importlib
from difflib import get_close_matches

import click
import typer
import typer.main
from typer import _click as _typer_click
from typer.core import TyperGroup

# Typer vendors its own copy of click, so at runtime command resolution raises
# `typer._click.exceptions.UsageError`, not the public `click` one. Catch both.
_USAGE_ERRORS: tuple[type[click.exceptions.UsageError], ...] = (
    click.exceptions.UsageError,
    _typer_click.exceptions.UsageError,
)

# Maps a top-level group name to "<module>:<typer-app-attr>". Populated by the root app.
LAZY_SUBCOMMANDS: dict[str, str] = {}

# Lazy group names that still dispatch but are omitted from `--help` (back-compat
# aliases). They resolve via `get_command` but never appear in `list_commands`.
LAZY_HIDDEN: set[str] = set()


class LazyTyperGroup(TyperGroup):
    """A `TyperGroup` whose `LAZY_SUBCOMMANDS` entries load on first access."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        """List eager commands (in registration order) then the lazy groups.

        The order mirrors the pre-split definition order so `--help` output is
        unchanged: top-level commands first, then the command groups.
        """
        eager = super().list_commands(ctx)
        lazy = [name for name in LAZY_SUBCOMMANDS if name not in eager and name not in LAZY_HIDDEN]
        return eager + lazy

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """Resolve a command, drawing typo suggestions from eager AND lazy names.

        Typer's base `resolve_command` matches "Did you mean ...?" candidates against
        `self.commands` only — the eagerly-registered commands — so the lazy groups would
        never be suggested. Reproduce Typer's own logic but match against the full
        `list_commands` set (names only; no module import is triggered). A single match
        over the union keeps suggestions identical to the pre-split CLI, including typos
        that are close to both an eager command and a lazy group.
        """
        try:
            return self._click_resolve_command(ctx, args)
        except _USAGE_ERRORS as exc:
            if self.suggest_commands and args:
                matches = get_close_matches(args[0], self.list_commands(ctx))
                if matches:
                    suggestions = ", ".join(f"{m!r}" for m in matches)
                    exc.message = f"{exc.message.rstrip('.')}. Did you mean {suggestions}?"
            raise

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Return a command, importing its module first when it is a lazy group."""
        if cmd_name in LAZY_SUBCOMMANDS:
            module_name, app_attr = LAZY_SUBCOMMANDS[cmd_name].split(":")
            module = importlib.import_module(module_name)
            command = typer.main.get_command(getattr(module, app_attr))
            # A materialized sub-Typer has no name of its own; the parent's help and
            # dispatch read it off the command, so stamp it from the registry key.
            command.name = cmd_name
            return command
        return super().get_command(ctx, cmd_name)
