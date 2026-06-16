"""Rules library: user-saved markdown snippets, optionally flagged as global defaults.

Storage layout:
- Body lives at `user_config_dir/rules/<name>.md`.
- Metadata (name, is_default, description) lives in the global SQLite DB.

`init` seeds default-flagged rules into the project's `.agent-init/rules/` dir
and adds their names to the manifest's `rules` list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import select

from agent_init.core import db, paths
from agent_init.core.models import RuleEntry

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class RuleNameError(ValueError):
    pass


class RuleNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    body: str
    description: str | None
    is_default: bool
    source: str = "local"  # "local" or the alias of the rule repo that supplied it


def _validate_name(name: str) -> None:
    # `fullmatch` (not `match`) so trailing whitespace / newlines from paste
    # accidents are rejected instead of silently accepted.
    if not _NAME_RE.fullmatch(name):
        raise RuleNameError(f"rule name {name!r} invalid: must be lowercase alphanumeric, _, or -")


def body_path(name: str) -> Path:
    return paths.rules_library_dir() / f"{name}.md"


def add(name: str, body: str, *, description: str | None = None, is_default: bool = False) -> Rule:
    _validate_name(name)
    paths.ensure_global_dirs()
    body_path(name).write_text(body)
    with db.session() as session:
        entry = session.get(RuleEntry, name)
        if entry is None:
            entry = RuleEntry(name=name, description=description, is_default=is_default)
        else:
            entry.description = description
            entry.is_default = is_default
        session.add(entry)
        session.commit()
    return Rule(name=name, body=body, description=description, is_default=is_default)


def get(name: str) -> Rule:
    """Resolve a rule by name. Local rules take priority over overlay sources."""
    with db.session() as session:
        entry = session.get(RuleEntry, name)
    if entry is not None:
        path = body_path(name)
        if path.exists():
            return Rule(
                name=name,
                body=path.read_text(),
                description=entry.description,
                is_default=entry.is_default,
                source="local",
            )
    # Fall back to overlay sources.
    from agent_init.core import rule_repos

    for alias, repo_dir in rule_repos.overlay_paths():
        candidate = repo_dir / f"{name}.md"
        if candidate.exists():
            return Rule(
                name=name,
                body=candidate.read_text(),
                description=None,
                is_default=False,
                source=alias,
            )
    raise RuleNotFoundError(name)


def list_all() -> list[Rule]:
    """List every accessible rule.

    Resolution order: local rules first (anything in the SQLite RuleEntry
    table with a body file); then, for each registered rule repo overlay,
    its .md files — but only if the name isn't already present (local wins).
    """
    paths.ensure_global_dirs()
    with db.session() as session:
        entries = list(session.exec(select(RuleEntry)).all())
    out: list[Rule] = []
    seen: set[str] = set()
    for entry in entries:
        path = body_path(entry.name)
        if not path.exists():
            continue
        out.append(
            Rule(
                name=entry.name,
                body=path.read_text(),
                description=entry.description,
                is_default=entry.is_default,
                source="local",
            )
        )
        seen.add(entry.name)

    from agent_init.core import rule_repos

    for alias, repo_dir in rule_repos.overlay_paths():
        for md in sorted(repo_dir.glob("*.md")):
            name = md.stem
            if name in seen:
                continue
            out.append(
                Rule(
                    name=name,
                    body=md.read_text(),
                    description=None,
                    is_default=False,
                    source=alias,
                )
            )
            seen.add(name)

    out.sort(key=lambda r: r.name)
    return out


def list_defaults() -> list[Rule]:
    return [r for r in list_all() if r.is_default]


def set_default(name: str, *, is_default: bool) -> None:
    with db.session() as session:
        entry = session.get(RuleEntry, name)
        if entry is None:
            raise RuleNotFoundError(name)
        entry.is_default = is_default
        session.add(entry)
        session.commit()


def delete(name: str) -> None:
    with db.session() as session:
        entry = session.get(RuleEntry, name)
        if entry is None:
            raise RuleNotFoundError(name)
        session.delete(entry)
        session.commit()
    path = body_path(name)
    if path.exists():
        path.unlink()


def apply_to_project(
    project_root: Path, names: list[str], *, rules_dir: Path | None = None
) -> list[Rule]:
    """Copy named rule bodies into the project's rules dir.
    Returns the resolved Rule objects in the order applied.

    Low-level primitive: writes files only. For the user-facing "install" flow
    (which also updates the manifest and re-renders AGENTS.md), use
    `install_to_project`.
    """
    if rules_dir is None:
        from agent_init.core.layout_profiles import resolve_active

        profile = resolve_active(project_root)
        resolved_rules_dir = project_root / profile.rules_dir
    else:
        resolved_rules_dir = rules_dir
    resolved_rules_dir.mkdir(parents=True, exist_ok=True)
    applied: list[Rule] = []
    for name in names:
        rule = get(name)
        (resolved_rules_dir / f"{name}.md").write_text(rule.body)
        applied.append(rule)
    return applied


def install_to_project(project_root: Path, name: str):  # type: ignore[no-untyped-def]
    """Add a rule to a project: copy body + re-render AGENTS.md.

    Same shape as skill install: idempotent, surfaces the project state to
    reflect the new rule. Re-uses `init` so mirror union semantics and drift
    detection apply automatically. Lazy import to avoid a rules<->init cycle.
    """
    # Sanity: rule must exist in the library.
    get(name)
    from agent_init.core import init as _init

    return _init.run(
        _init.InitOptions(
            project_root=project_root,
            extra_rules=[name],
            seed_default_rules=False,
        )
    )
