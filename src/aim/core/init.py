"""`aim init` orchestration: create or update the user-editable `aim.toml`.

`init` does NOT write AGENTS.md, symlinks, or `aim.lock.toml`. Those are produced by
`aim lock` and `aim sync` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aim.core import (
    archetypes,
    declarations,
    layout_profiles,
    paths,
    policy,
    repos,
    templates,
)
from aim.core.models import DeclaredArchetype, ProjectDeclarations
from aim.core.validation import MirrorNameError, is_valid_mirror_name

KNOWN_SYMLINKS = ("CLAUDE.md", "GEMINI.md", "OPENCODE.md")
# Sentinel for `instruction_archetype`: explicitly select the built-in template
# (clears any existing archetype). None means "leave the current selection as-is".
BUILTIN_INSTRUCTIONS = "builtin"


@dataclass
class InitOptions:
    """Configuration inputs for an `aim init` run."""

    project_root: Path
    symlinks: tuple[str, ...] = ()
    clear_symlinks: bool = False
    layout_profile: str | None = None
    # Selected instruction archetype qualified name, or None for the built-in template.
    instruction_archetype: str | None = None


@dataclass
class InitResult:
    """Outcome of an `aim init` run."""

    project_root: Path
    declarations_path: Path
    applied_rules: list[str]
    re_init: bool


def run(options: InitOptions) -> InitResult:
    """Create or update the project's `aim.toml` declarations.

    Args:
        options: Project root and the template, symlink, and layout choices to apply.

    Returns:
        An InitResult describing the written declarations and whether this was a re-init.

    Raises:
        MirrorNameError: If a requested symlink filename is not a valid mirror name.
    """
    paths.ensure_global_dirs()
    templates.ensure_builtin_registered()

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
    decl = declarations.load(proj) if re_init else ProjectDeclarations()

    # Resolve layout profile: CLI option wins, then existing decl.
    active_profile_name = options.layout_profile or decl.layout_profile
    active_profile = (
        layout_profiles.get_profile(proj, active_profile_name)
        if active_profile_name
        else layout_profiles.BUILTIN_CLAUDE
    )
    # Check the EFFECTIVE profile name (never None) against the allow-list.
    policy.assert_profile_allowed(policy.effective_policy(proj), active_profile.name)

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
    decl.layout_profile = options.layout_profile or decl.layout_profile
    decl.symlinks = requested_symlinks
    # Record the selected instruction archetype. None leaves the current selection
    # untouched; BUILTIN_INSTRUCTIONS clears it (use the built-in template); a
    # qualified name selects an archetype, pinned + rendered at the first lock/sync.
    if options.instruction_archetype == BUILTIN_INSTRUCTIONS:
        decl.instruction_archetype = None
    elif options.instruction_archetype is not None:
        row = archetypes.index_row(options.instruction_archetype)
        policy.assert_archetype_allowed(policy.effective_policy(proj), row.qualified_name)
        decl.instruction_archetype = DeclaredArchetype(
            qualified_name=row.qualified_name,
            repo_alias=row.repo_alias,
            source_path=row.instruction_path,
        )
        decl.repos[row.repo_alias] = repos.get(row.repo_alias).url
    # Seed a default (permissive) local policy on first init so governance is
    # discoverable and editable in aim.toml; preserve any existing [policy].
    if not decl.policy:
        decl.policy = {"scope": "local"}

    declarations.save(proj, decl)
    return InitResult(
        project_root=proj,
        declarations_path=decl_path,
        applied_rules=[r.qualified_name for r in decl.rules],
        re_init=re_init,
    )
