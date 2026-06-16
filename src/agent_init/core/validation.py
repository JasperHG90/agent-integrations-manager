"""Shared validation helpers used across core modules.

Kept in one place to avoid circular imports between modules that need the same
validators (e.g. `init.py` and `layout_profiles.py`).
"""

from __future__ import annotations

import re

_MIRROR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.md$")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class MirrorNameError(ValueError):
    """A mirror filename failed validation (path traversal, bad chars, etc.)."""


class AliasNameError(ValueError):
    """An alias/name failed validation (bad chars, reserved, etc.)."""


def is_valid_mirror_name(name: str) -> bool:
    """Reject `../etc/passwd.md`, absolute paths, weird chars.

    Rules: letters/digits/`_`/`-`/`.` only, must start with alnum, must end
    with `.md`. Single segment (no `/` or `\\`).
    """
    if "/" in name or "\\" in name:
        return False
    return bool(_MIRROR_NAME_RE.fullmatch(name))


def is_valid_alias(name: str) -> bool:
    """Validate a repo/MCP alias.

    Rules: lowercase letters, digits, `_`, `-`; must start with alnum.
    """
    return bool(_ALIAS_RE.fullmatch(name))


def is_valid_agent_name(name: str) -> bool:
    """Validate a sub-agent directory/file name.

    Mirrors alias rules to avoid `.`, `..`, path separators, and shell-special
    characters ending up in `.claude/agents/<name>.md`.
    """
    return bool(_ALIAS_RE.fullmatch(name))


def is_valid_rule_name(name: str) -> bool:
    """Validate a rule file stem.

    Mirrors alias rules to avoid `.`, `..`, path separators, and shell-special
    characters ending up in `.claude/rules/<name>.md`.
    """
    return bool(_ALIAS_RE.fullmatch(name))
