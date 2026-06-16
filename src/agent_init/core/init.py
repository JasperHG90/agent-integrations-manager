"""`agent-init init` orchestration: render AGENTS.md, optionally write mirror
files (CLAUDE.md, GEMINI.md, etc.), seed default rules, write manifest.

On re-init, in-region content edited by hand is detected via stored region
hashes; we emit a warning but still overwrite (the plan's accepted behavior).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_init.core import (
    agents_md,
    hashing,
    layout_profiles,
    manifest,
    paths,
    rule_compose,
    rules,
    templates,
)
from agent_init.core.models import Manifest
from agent_init.core.validation import MirrorNameError, is_valid_mirror_name

KNOWN_MIRRORS = ("CLAUDE.md", "GEMINI.md", "OPENCODE.md")
DEFAULT_MIRRORS: tuple[str, ...] = ()  # opt-in only — `init` writes mirrors you ask for

# Map mirror filename -> agent dialect string passed to template rendering.
# Templates can do `{% if agent == "claude" %}...{% endif %}` to customise
# per-mirror content. AGENTS.md gets `agent=None`.
_AGENT_FROM_FILENAME = {
    "AGENTS.md": None,
    "CLAUDE.md": "claude",
    "GEMINI.md": "gemini",
    "OPENCODE.md": "opencode",
    "CURSOR.md": "cursor",
}


def agent_for_filename(filename: str) -> str | None:
    return _AGENT_FROM_FILENAME.get(filename)


@dataclass
class InitOptions:
    project_root: Path
    template: str = templates.BUILTIN_DEFAULT
    # Mirrors to ensure exist alongside AGENTS.md. On re-init, this is UNIONed
    # with the prior `managed_files` so a bare `init` never silently drops
    # mirrors a user already had. To remove a mirror, delete the file and
    # re-init, or use `clear_mirrors=True`.
    mirrors: tuple[str, ...] = DEFAULT_MIRRORS
    symlinks: tuple[str, ...] = ()
    clear_mirrors: bool = False
    seed_default_rules: bool = True
    extra_rules: list[str] = field(default_factory=list)
    force: bool = False
    agent_dialect: str | None = None
    dry_run: bool = False
    # Name of a layout profile to use; overrides manifest.layout_profile.
    layout_profile: str | None = None


@dataclass
class FileChange:
    """A pending file write — `before` is None for newly created files."""

    path: Path
    before: str | None
    after: str


@dataclass
class InitResult:
    project_root: Path
    agents_md_path: Path
    mirror_paths: list[Path]
    symlink_paths: list[Path]
    applied_rules: list[str]
    manifest_path: Path
    re_init: bool
    region_drift_warnings: list[str] = field(default_factory=list)
    pending_changes: list[FileChange] = field(default_factory=list)
    dry_run: bool = False


def run(options: InitOptions) -> InitResult:
    paths.ensure_global_dirs()
    templates.ensure_builtin_registered()

    # Validate template up-front so we don't half-create state on failure.
    templates.resolve(options.template)

    # Validate mirror filenames before touching the filesystem — reject path
    # traversal, absolute paths, weird chars.
    for mirror in options.mirrors:
        if not is_valid_mirror_name(mirror):
            raise MirrorNameError(
                f"mirror filename {mirror!r} invalid: must match "
                "[A-Za-z0-9][A-Za-z0-9_.-]*.md and be a single path segment"
            )

    proj = options.project_root
    proj.mkdir(parents=True, exist_ok=True)

    existing_manifest_path = paths.project_manifest_path(proj)
    re_init = existing_manifest_path.exists()
    m = manifest.load(proj) if re_init else Manifest(template=options.template)

    # Resolve the active layout profile. CLI option wins, then manifest, then legacy.
    active_profile_name = options.layout_profile or m.layout_profile
    if active_profile_name:
        active_profile = layout_profiles.get_profile(proj, active_profile_name)
    else:
        active_profile = layout_profiles.LEGACY_PROFILE

    # Mirror semantics: opt-in on first init; on re-init, UNION with prior
    # managed_files so a `init` invocation that just adds a rule doesn't
    # silently drop CLAUDE.md/GEMINI.md the user already had. On first init,
    # fall back to the profile's default mirrors when no mirrors are supplied.
    requested_mirrors = options.mirrors
    requested_symlinks = options.symlinks
    if not re_init and not requested_mirrors:
        requested_mirrors = tuple(active_profile.mirrors)
    if not re_init and not requested_symlinks:
        requested_symlinks = tuple(active_profile.symlinks)
    if re_init and not options.clear_mirrors:
        prior_mirrors = tuple(
            name
            for name in m.managed_files
            if name.lower() != active_profile.agents_md.lower() and is_valid_mirror_name(name)
        )
        merged_mirrors = list(dict.fromkeys((*requested_mirrors, *prior_mirrors)))
        merged_symlinks = list(dict.fromkeys((*requested_symlinks, *prior_mirrors)))
        # If the manifest has no prior managed files (e.g. it was only created by
        # set_active), apply the profile's default mirrors/symlinks on first real init.
        if not merged_mirrors and not requested_mirrors:
            merged_mirrors = list(active_profile.mirrors)
        if not merged_symlinks and not requested_symlinks:
            merged_symlinks = list(active_profile.symlinks)
        effective_mirrors: tuple[str, ...] = tuple(merged_mirrors)
        effective_symlinks: tuple[str, ...] = tuple(merged_symlinks)
    else:
        effective_mirrors = requested_mirrors
        effective_symlinks = requested_symlinks

    # Resolve which rules to apply.
    rule_names: list[str] = []
    if re_init:
        rule_names = list(m.rules)
    if options.seed_default_rules:
        for r in rules.list_defaults():
            if r.name not in rule_names:
                rule_names.append(r.name)
    for name in options.extra_rules:
        if name not in rule_names:
            rule_names.append(name)

    # Expand transitive `extends:` and reorder by `order:` front-matter.
    expanded_names = rule_compose.resolve(rule_names, lambda n: rules.get(n).body)
    rule_names = expanded_names
    # Resolve applied rules (with their bodies) WITHOUT writing — write only
    # if we're going to commit. This lets `dry_run` skip side effects.
    applied = [rules.get(name) for name in rule_names]

    def _render_for_agent(agent: str | None) -> str:
        return templates.render(options.template, {"rules": applied, "agent": agent})

    def _regions_for_agent(agent: str | None) -> dict[str, str]:
        rendered = _render_for_agent(agent)
        return {r.name: r.body for r in agents_md.parse(rendered)}

    # Render fresh AGENTS.md regions (no agent dialect).
    fresh_regions_canonical = _regions_for_agent(None)

    pending: list[FileChange] = []

    agents_path = proj / active_profile.agents_md
    drift_warnings: list[str] = []
    if agents_path.exists() and not options.force:
        existing = agents_path.read_text()
        drift_warnings.extend(
            _detect_region_drift(agents_path.name, existing, m.managed_region_hashes)
        )
        merged = agents_md.merge(existing, fresh_regions_canonical)
        if merged != existing:
            pending.append(FileChange(path=agents_path, before=existing, after=merged))
    else:
        merged = _render_for_agent(None)
        before = agents_path.read_text() if agents_path.exists() else None
        if before != merged:
            pending.append(FileChange(path=agents_path, before=before, after=merged))

    # Hashes will be computed against `merged` regardless of whether we apply.
    new_hashes = {r.name: hashing.hash_text(r.body) for r in agents_md.parse(merged)}

    mirror_paths: list[Path] = []
    symlink_paths: list[Path] = []
    mirror_render_cache: dict[str | None, tuple[str, dict[str, str]]] = {
        None: (merged, fresh_regions_canonical)
    }

    def _render_mirror(agent: str | None) -> tuple[str, dict[str, str]]:
        if agent not in mirror_render_cache:
            rendered_for_agent = _render_for_agent(agent)
            regions_for_agent = {r.name: r.body for r in agents_md.parse(rendered_for_agent)}
            mirror_render_cache[agent] = (rendered_for_agent, regions_for_agent)
        return mirror_render_cache[agent]

    for mirror in effective_mirrors:
        target = proj / mirror
        agent = agent_for_filename(mirror)
        rendered_for_mirror, regions_for_mirror = _render_mirror(agent)
        if target.exists() and target.resolve() == agents_path.resolve():
            mirror_paths.append(target)
            continue
        if target.exists() and not target.is_symlink():
            mirror_text = target.read_text()
            if "<!-- BEGIN agent-init:" in mirror_text:
                drift_warnings.extend(
                    _detect_region_drift(target.name, mirror_text, m.managed_region_hashes)
                )
                after = agents_md.merge(mirror_text, regions_for_mirror)
                if after != mirror_text:
                    pending.append(FileChange(path=target, before=mirror_text, after=after))
                mirror_paths.append(target)
                continue
            if options.force:
                drift_warnings.append(
                    f"force-overwrote {target.name} (had no agent-init markers; "
                    "any hand-written content was lost)"
                )
                pending.append(
                    FileChange(path=target, before=mirror_text, after=rendered_for_mirror)
                )
                mirror_paths.append(target)
                continue
            drift_warnings.append(
                f"{target.name} exists with no agent-init markers; left as-is (use --force to overwrite)"
            )
            mirror_paths.append(target)
            continue
        # New file (or stale symlink).
        before = None
        if target.is_symlink():
            before = "<symlink>"
        pending.append(FileChange(path=target, before=before, after=rendered_for_mirror))
        mirror_paths.append(target)

    for link_name in effective_symlinks:
        target = proj / link_name
        symlink_paths.append(target)
        if target.exists() and target.resolve() == agents_path.resolve():
            continue
        if target.exists() and not options.force:
            drift_warnings.append(
                f"{target.name} exists; left as-is (use --force to overwrite)"
            )
            continue
        if target.exists() or target.is_symlink():
            target.unlink()
        before = "<symlink>" if target.is_symlink() else None
        pending.append(
            FileChange(path=target, before=before, after=f"<symlink to {active_profile.agents_md}>")
        )

    # Actual symlink creation happens during commit, after AGENTS.md exists.

    # Rules: dry-run also collects each rule-body write.
    project_rules_dir = proj / active_profile.rules_dir
    for rule in applied:
        rule_path = project_rules_dir / f"{rule.name}.md"
        before = rule_path.read_text() if rule_path.exists() else None
        if before != rule.body:
            pending.append(FileChange(path=rule_path, before=before, after=rule.body))

    if options.dry_run:
        return InitResult(
            project_root=proj,
            agents_md_path=agents_path,
            mirror_paths=mirror_paths,
            symlink_paths=symlink_paths,
            applied_rules=[r.name for r in applied],
            manifest_path=existing_manifest_path,
            re_init=re_init,
            region_drift_warnings=drift_warnings,
            pending_changes=pending,
            dry_run=True,
        )

    # --- COMMIT: apply pending changes ---
    rules.apply_to_project(proj, rule_names, rules_dir=project_rules_dir)
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    agents_path.write_text(merged)
    for mirror in effective_mirrors:
        target = proj / mirror
        agent = agent_for_filename(mirror)
        rendered_for_mirror, regions_for_mirror = mirror_render_cache[agent]
        if target.exists() and target.resolve() == agents_path.resolve():
            continue
        if target.is_symlink():
            target.unlink()
        if target.exists():
            mirror_text = target.read_text()
            if "<!-- BEGIN agent-init:" in mirror_text:
                target.write_text(agents_md.merge(mirror_text, regions_for_mirror))
                continue
            if options.force:
                target.unlink()
                target.write_text(rendered_for_mirror)
                continue
            # left as-is per drift_warnings above
            continue
        target.write_text(rendered_for_mirror)

    # Create/refresh symlinks to agents_md.
    for link_name in effective_symlinks:
        target = proj / link_name
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(agents_path.name)

    # Finalize manifest.
    m.template = options.template
    m.rules = rule_names
    managed = [active_profile.agents_md, *(p.name for p in mirror_paths), *(p.name for p in symlink_paths)]
    m.managed_files = list(dict.fromkeys(managed))
    m.managed_region_hashes = new_hashes
    if options.agent_dialect is not None:
        m.agent_dialect = options.agent_dialect or None
    m.layout_profile = active_profile.name
    manifest.save(proj, m)

    return InitResult(
        project_root=proj,
        agents_md_path=agents_path,
        mirror_paths=mirror_paths,
        symlink_paths=symlink_paths,
        applied_rules=[r.name for r in applied],
        manifest_path=existing_manifest_path,
        re_init=re_init,
        region_drift_warnings=drift_warnings,
        pending_changes=pending,
    )


def _render_regions(template_name: str, applied_rules: list[rules.Rule]) -> dict[str, str]:
    rendered = templates.render(template_name, {"rules": applied_rules})
    regions = agents_md.parse(rendered)
    return {r.name: r.body for r in regions}


def _region_hashes(text: str) -> dict[str, str]:
    return {r.name: hashing.hash_text(r.body) for r in agents_md.parse(text)}


def _detect_region_drift(filename: str, body: str, stored_hashes: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    for region in agents_md.parse(body):
        prior = stored_hashes.get(region.name)
        if prior is None:
            continue
        if hashing.hash_text(region.body) != prior:
            warnings.append(
                f"{filename}: in-region content of `{region.name}` was edited "
                "since last write; overwriting"
            )
    return warnings
