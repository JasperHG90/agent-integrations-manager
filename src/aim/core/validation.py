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
    """Validate a mirror filename, rejecting path traversal and bad chars.

    Rules: letters/digits/`_`/`-`/`.` only, must start with alnum, must end
    with `.md`, and must be a single segment (no `/` or `\\`).

    Args:
        name: Candidate mirror filename.

    Returns:
        True if the name is a safe single-segment `.md` filename.
    """
    if "/" in name or "\\" in name:
        return False
    return bool(_MIRROR_NAME_RE.fullmatch(name))


def is_valid_alias(name: str) -> bool:
    """Validate a repo/MCP alias.

    Rules: lowercase letters, digits, `_`, `-`; must start with alnum.

    Args:
        name: Candidate alias.

    Returns:
        True if the alias matches the allowed pattern.
    """
    return bool(_ALIAS_RE.fullmatch(name))


def is_valid_agent_name(name: str) -> bool:
    """Validate a sub-agent directory/file name.

    Mirrors alias rules to avoid `.`, `..`, path separators, and shell-special
    characters ending up in `.claude/agents/<name>.md`.

    Args:
        name: Candidate sub-agent name.

    Returns:
        True if the name matches the allowed pattern.
    """
    return bool(_ALIAS_RE.fullmatch(name))


def is_valid_rule_name(name: str) -> bool:
    """Validate a rule file stem.

    Mirrors alias rules to avoid `.`, `..`, path separators, and shell-special
    characters ending up in `.claude/rules/<name>.md`.

    Args:
        name: Candidate rule file stem.

    Returns:
        True if the name matches the allowed pattern.
    """
    return bool(_ALIAS_RE.fullmatch(name))


def is_valid_archetype_name(name: str) -> bool:
    """Validate a project-instruction archetype directory name.

    Same constraints as other artifact names, so a qualified name like
    `<alias>/<archetype>` is shell- and path-safe.

    Args:
        name: Candidate archetype directory name.

    Returns:
        True if the name matches the allowed pattern.
    """
    return bool(_ALIAS_RE.fullmatch(name))


def is_safe_repo_path(path: str) -> bool:
    """Check whether a path is safe to pass back to git as a pathspec.

    Rejects absolute paths, `..` segments, and the git pathspec-magic prefix
    `:(`.

    Args:
        path: Candidate repo-relative path.

    Returns:
        True if the path is safe to use as a git pathspec.
    """
    if not path:
        return True
    if path.startswith(("/", "\\")):
        return False
    if ":(" in path:
        return False
    return all(part != ".." for part in path.split("/"))
