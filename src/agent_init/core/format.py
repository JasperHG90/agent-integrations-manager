"""CLI output formatting: Rich tables or JSON.

The TUI is the fancy surface; the CLI is scripting/CI-first. This module lets
list commands render as tables by default and as JSON when `--json` is passed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table


class OutputFormat:
    TABLE = "table"
    JSON = "json"


def _serialize(value: object) -> Any:
    """Flatten common non-JSON types for JSON output."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _serialize(v) for k, v in asdict(value).items()}  # type: ignore[arg-type]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def render_table(
    rows: list[Any],
    *,
    title: str | None = None,
    columns: list[str] | None = None,
    row_extractor: dict[str, str] | None = None,
) -> None:
    """Render rows as a Rich table, or print a friendly message if empty.

    Args:
        rows: list of objects to render.
        title: table title (also used in the empty message).
        columns: ordered list of column headers.
        row_extractor: mapping from column header -> attribute name on the row.
            If omitted, derived from `columns` (column header == attribute name).
    """
    if not rows:
        typer.echo(f"no {title or 'rows'}" if title else "no rows")
        return

    cols = columns or []
    extractor = row_extractor or {}
    if not cols:
        first = rows[0]
        if isinstance(first, dict):
            cols = list(first.keys())
            extractor = {k: k for k in cols}
        elif isinstance(first, BaseModel):
            cols = list(type(first).model_fields.keys())
            extractor = {k: k for k in cols}
        elif is_dataclass(first) and not isinstance(first, type):
            cols = [f.name for f in first.__dataclass_fields__.values()]
            extractor = {k: k for k in cols}
        else:
            cols = ["value"]
            extractor = {"value": "value"}

    table = Table(title=title)
    for col in cols:
        table.add_column(col, overflow="fold")
    for row in rows:
        table.add_row(*[_get_attr(row, extractor.get(col, col)) for col in cols])
    Console().print(table)


def _get_attr(row: Any, attr: str) -> str:
    """Resolve an attribute path like `current.identifier` to a string."""
    value: Any = row
    for part in attr.split("."):
        if value is None:
            return ""
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
    return _cell(value)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def render_json(rows: list[Any]) -> None:
    """Print rows as a JSON list (empty list prints `[]`)."""
    typer.echo(json.dumps(_serialize(rows), indent=2))


def render(
    rows: list[Any],
    format: str,
    *,
    title: str | None = None,
    columns: list[str] | None = None,
    row_extractor: dict[str, str] | None = None,
) -> None:
    """Render rows as a table or JSON depending on `format`."""
    if format == OutputFormat.JSON:
        render_json(rows)
    else:
        render_table(rows, title=title, columns=columns, row_extractor=row_extractor)
