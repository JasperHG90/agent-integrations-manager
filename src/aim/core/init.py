"""`aim init` orchestration: create or update the user-editable `aim.toml`.

`init` does NOT write AGENTS.md, symlinks, or `aim.lock.toml`. Those are produced by
`aim lock` and `aim sync` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aim.core import (
    declarations,
    layout_profiles,
    paths,
    templates,
)
from aim.core.models import ProjectDeclarations
from aim.core.validation import MirrorNameError, is_valid_mirror_name

KNOWN_SYMLINKS = ("CLAUDE.md", "GEMINI.md", "OPENCODE.md")


@dataclass
class InitOptions:
    project_root: Path
    instruction_template: str = templates.BUILTIN_DEFAULT
    symlinks: tuple[str, ...] = ()
    clear_symlinks: bool = False
    layout_profile: str | None = None


@dataclass
class InitResult:
    project_root: Path
    declarations_path: Path
    applied_rules: list[str]
    re_init: bool


def run(options: InitOptions) -> InitResult:
    paths.ensure_global_dirs()
    templates.ensure_builtin_registered()
    templates.resolve(options.instruction_template)

    for link in options.symlinks:
        if not is_valid_mirror_name(link):
            raise MirrorNameError(
                f"symlink filename {link!r} invalid: must match "
                "[A-Za-z0-9][A-Za-z0-9_.-]*.md and be a single path segment"
            )

    proj = options.project_root
    proj.mkdir(parents=True, exist_ok=True)

    decl_path = paths.project_declarations_path(proj)
    re_init = decl_path.exists()
    decl = (
        declarations.load(proj)
        if re_init
        else ProjectDeclarations(instruction_template=options.instruction_template)
    )

    # Resolve layout profile: CLI option wins, then existing decl.
    active_profile_name = options.layout_profile or decl.layout_profile
    active_profile = (
        layout_profiles.get_profile(proj, active_profile_name)
        if active_profile_name
        else layout_profiles.BUILTIN_CLAUDE
    )

    # Symlink semantics: on first init fall back to profile defaults.
    requested_symlinks = list(options.symlinks)
    if not re_init and not requested_symlinks:
        requested_symlinks = list(active_profile.symlinks)

    # On re-init, union with existing declarations unless clearing.
    if re_init and not options.clear_symlinks:
        existing_symlinks = list(decl.symlinks)
        requested_symlinks = list(dict.fromkeys([*existing_symlinks, *requested_symlinks]))

    # Rules are repo-sourced artifacts managed by `aim rule add`; init only
    # preserves any already-declared rules (it never resolves or seeds them).
    decl.instruction_template = options.instruction_template
    decl.layout_profile = options.layout_profile or decl.layout_profile
    decl.symlinks = requested_symlinks

    declarations.save(proj, decl)
    return InitResult(
        project_root=proj,
        declarations_path=decl_path,
        applied_rules=[r.qualified_name for r in decl.rules],
        re_init=re_init,
    )
