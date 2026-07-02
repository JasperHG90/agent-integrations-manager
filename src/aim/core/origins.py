"""Provenance (origin) of discovered artifacts and the visibility rules over it.

Discovery records WHERE an artifact was found in its source repo:

- ``canonical``  — a rank-0 location (``skills/``, ``agents/``, ``rules/``,
  ``hooks/``, or the repo root).
- ``dot-claude`` — a rank-1 ``.claude/<surface>/`` location.
- ``other``      — a rank-2 arbitrary path (skills/agents only; rules and
  hooks are only discovered under their two prefixes).
- ``plugin``     — inside a dir-kind plugin's source dir (bundled with the
  plugin, indexed for visibility but hidden from lists by default).

Only surfaces with these dimensions carry an origin: skills, agents, rules,
and hooks. Plugins and targets have no ``.claude``/plugin-owned dimension.

Index rows written before the origin column existed have ``origin = NULL``;
those legacy indexes contain no plugin-owned rows by construction (they were
dropped at discovery), so the origin can be derived exactly from the path's
prefix rank via `derive_origin`.
"""

from __future__ import annotations

from collections.abc import Callable

ORIGIN_CANONICAL = "canonical"
ORIGIN_DOT_CLAUDE = "dot-claude"
ORIGIN_OTHER = "other"
ORIGIN_PLUGIN = "plugin"

_RANK_TO_ORIGIN = {0: ORIGIN_CANONICAL, 1: ORIGIN_DOT_CLAUDE, 2: ORIGIN_OTHER}


def origin_from_rank(rank: int) -> str:
    """Map a surface's prefix rank (0/1/2, lower is more canonical) to an origin."""
    return _RANK_TO_ORIGIN.get(rank, ORIGIN_OTHER)


def derive_origin(row_origin: str | None, path: str, rank_fn: Callable[[str], int]) -> str:
    """Return a row's effective origin, deriving it for legacy (NULL-origin) rows.

    Args:
        row_origin: The stored origin column value (None on pre-origin rows).
        path: The row's indexed source path relative to the repo root.
        rank_fn: The surface's prefix-rank function (e.g. ``skills._prefix_rank``).

    Returns:
        The stored origin, or one derived from the path's prefix rank.
    """
    if row_origin is not None:
        return row_origin
    return origin_from_rank(rank_fn(path))


def is_visible(origin: str, *, include_plugin_owned: bool, include_dot_claude: bool) -> bool:
    """Whether an artifact with this origin appears in a list/search result.

    Canonical and ``other`` origins are always visible. Plugin-owned rows are
    hidden unless ``include_plugin_owned``; ``.claude/``-found rows are hidden
    when ``include_dot_claude`` is False.
    """
    if origin == ORIGIN_PLUGIN:
        return include_plugin_owned
    if origin == ORIGIN_DOT_CLAUDE:
        return include_dot_claude
    return True
