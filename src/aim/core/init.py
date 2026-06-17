"""`aim init` orchestration: create or update the user-editable `aim.toml`.

`init` does NOT write AGENTS.md, symlinks, or `aim.lock.toml`. Those are produced by
`aim lock` and `aim sync` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aim.core import (
    declarations,
    layout_profiles,
    paths,
    rule_compose,
    rules,
    templates,
    validation,
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
    extra_rules: list[str] = field(default_factory=list)
    layout_profile: str | None = None
    extra_rule_files: dict[str, Path] = field(default_factory=dict)


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

    # Seed rules from explicit files into the global library so re-init works.
    for name, path in options.extra_rule_files.items():
        if not validation.is_valid_rule_name(name):
            raise rules.RuleNameError(
                f"rule-file name {name!r} invalid: must be lowercase alphanumeric, _, or -"
            )
        body = path.read_text(encoding="utf-8")
        rules.add(name, body, description=None, is_default=False)

    # Resolve which rules to record.
    rule_names: list[str] = []
    if re_init:
        rule_names = list(decl.rules)
    for name in options.extra_rules:
        if name not in rule_names:
            rule_names.append(name)
    for name in options.extra_rule_files:
        if name not in rule_names:
            rule_names.append(name)

    expanded_names = rule_compose.resolve(rule_names, lambda n: rules.get(n).body)
    rule_names = expanded_names

    # Update declaration model.
    decl.instruction_template = options.instruction_template
    decl.layout_profile = options.layout_profile or decl.layout_profile
    decl.rules = rule_names
    decl.symlinks = requested_symlinks

    declarations.save(proj, decl)
    return InitResult(
        project_root=proj,
        declarations_path=decl_path,
        applied_rules=rule_names,
        re_init=re_init,
    )
