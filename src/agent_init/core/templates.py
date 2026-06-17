"""Template registry. Bundled default plus user-registered templates.

Per the plan: pluggable from day one. The `default` template is bundled and
re-seeded into the global SQLite registry on demand. The bundled default is also
copied to the user's global templates directory so it can be edited from the
TUI; `resolve()` prefers that override when it exists.

Users can add their own via `agent-init template add` (CLI lands in a later
phase if there is a second template; the API is wired up now so it's an
additive change).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, StrictUndefined
from sqlmodel import select

from agent_init.core import db, paths
from agent_init.core.models import Template

BUILTIN_DEFAULT = "default"
_BUILTIN_PACKAGE = "agent_init.templates"


class TemplateNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class ResolvedTemplate:
    name: str
    body: str  # raw jinja source


def _builtin_body(template_name: str) -> str:
    resource = files(_BUILTIN_PACKAGE).joinpath(f"{template_name}.md.j2")
    if not resource.is_file():
        raise TemplateNotFoundError(template_name)
    return resource.read_text()


def _builtin_override_path(template_name: str) -> Path:
    """Filesystem path where a user-editable copy of a builtin template lives."""
    paths.ensure_global_dirs()
    return paths.templates_library_dir() / f"{template_name}.md.j2"


def _ensure_builtin_override(template_name: str) -> Path:
    """Copy the bundled template to the global templates dir if no override exists."""
    override = _builtin_override_path(template_name)
    if not override.exists():
        override.write_text(_builtin_body(template_name), encoding="utf-8")
    return override


def ensure_builtin_registered() -> None:
    """Idempotently register the bundled default template in the global DB."""
    with db.session() as session:
        existing = session.get(Template, BUILTIN_DEFAULT)
        if existing is None:
            session.add(
                Template(
                    name=BUILTIN_DEFAULT,
                    source="builtin",
                    description="Default agent-init template (managed regions for header + rules).",
                )
            )
            session.commit()
    _ensure_builtin_override(BUILTIN_DEFAULT)


def list_templates() -> list[Template]:
    ensure_builtin_registered()
    with db.session() as session:
        return list(session.exec(select(Template)).all())


def resolve(name: str) -> ResolvedTemplate:
    ensure_builtin_registered()
    with db.session() as session:
        row = session.get(Template, name)
    if row is None:
        raise TemplateNotFoundError(name)
    if row.source == "builtin":
        # Prefer the user-editable override if it exists.
        override = _builtin_override_path(name)
        if override.is_file():
            return ResolvedTemplate(name=name, body=override.read_text(encoding="utf-8"))
        return ResolvedTemplate(name=name, body=_builtin_body(name))
    # User-registered template: source is a filesystem path to a .md.j2 file.
    path = Path(row.source)
    if not path.is_file():
        raise TemplateNotFoundError(f"template {name!r} source missing: {path}")
    return ResolvedTemplate(name=name, body=path.read_text())


def register_user_template(name: str, source_path: Path, description: str | None = None) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    with db.session() as session:
        existing = session.get(Template, name)
        if existing is not None:
            existing.source = str(source_path)
            existing.description = description
            session.add(existing)
        else:
            session.add(
                Template(name=name, source=str(source_path), description=description)
            )
        session.commit()


def render(template_name: str, context: dict) -> str:
    template = resolve(template_name)
    env = Environment(
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    return env.from_string(template.body).render(**context)
