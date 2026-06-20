"""Render and write AGENTS.md and symlinks.

Shared between `aim lock` (to compute region hashes) and `aim sync` (to restore
files on disk). The caller owns the Manifest/Lockfile object; this module only
reads from it and updates managed-file/region-hash bookkeeping.
"""

from __future__ import annotations

from pathlib import Path

from aim.core import agents_md, content_guard, hashing, layout_profiles, repo_rules, templates
from aim.core.models import RenderRule

_AGENT_FROM_FILENAME = {
    "AGENTS.md": None,
    "CLAUDE.md": "claude",
    "GEMINI.md": "gemini",
    "OPENCODE.md": "opencode",
    "CURSOR.md": "cursor",
}


def agent_for_filename(filename: str) -> str | None:
    """Return the agent slug for a managed instruction filename, or None for AGENTS.md.

    Args:
        filename: Basename of an instruction file, e.g. ``CLAUDE.md``.

    Returns:
        The agent slug (e.g. ``"claude"``) or None if the filename is unknown or is
        the canonical ``AGENTS.md``.
    """
    return _AGENT_FROM_FILENAME.get(filename)


def _detect_region_drift(filename: str, body: str, stored_hashes: dict[str, str]) -> list[str]:
    """Compare on-disk region content against stored hashes and report edited regions.

    Args:
        filename: Name of the file being checked, used in warning messages.
        body: Full current text of the file on disk.
        stored_hashes: Previously recorded hash per managed region name.

    Returns:
        A warning string for each managed region whose in-region content changed
        since the last write.

    Raises:
        agents_md.RegionError: If the file's aim region markers are malformed.
    """
    warnings: list[str] = []
    try:
        regions = agents_md.parse(body)
    except agents_md.RegionError as exc:
        raise agents_md.RegionError(f"{filename}: malformed aim region markers — {exc}") from exc
    for region in regions:
        prior = stored_hashes.get(region.name)
        if prior is None:
            continue
        if hashing.hash_text(region.body) != prior:
            warnings.append(
                f"{filename}: in-region content of `{region.name}` was edited "
                "since last write; overwriting"
            )
    return warnings


def _render_regions(
    template_name: str,
    applied_rules: list[RenderRule],
    *,
    rules_mode: str,
) -> dict[str, str]:
    """Render a template and return its region bodies keyed by region name.

    Args:
        template_name: Name of the instruction template to render.
        applied_rules: Rules already prepared for rendering.
        rules_mode: Rendering mode for rules, supplied by the layout profile.

    Returns:
        A mapping of region name to its rendered body.
    """
    rendered = templates.render(
        template_name,
        {"rules": applied_rules, "rules_mode": rules_mode},
    )
    regions = agents_md.parse(rendered)
    return {r.name: r.body for r in regions}


def _render_for_template(
    template_name: str,
    applied_rules: list[RenderRule],
    *,
    rules_mode: str,
) -> str:
    """Render a template to its full canonical text.

    Args:
        template_name: Name of the instruction template to render.
        applied_rules: Rules already prepared for rendering.
        rules_mode: Rendering mode for rules, supplied by the layout profile.

    Returns:
        The fully rendered instruction file text.
    """
    return templates.render(
        template_name,
        {"rules": applied_rules, "rules_mode": rules_mode},
    )


def write_agent_files(
    project_root: Path,
    m,
    profile: layout_profiles.LayoutProfile,
    *,
    force: bool = False,
) -> list[str]:
    """Render AGENTS.md plus its symlinks to disk and return drift warnings.

    Args:
        project_root: Directory the managed files are written into.
        m: The Manifest describing rules, template, symlinks, and stored hashes.
        profile: Layout profile selecting the AGENTS.md path and rules mode.
        force: Overwrite existing files even when their content has drifted.

    Returns:
        Warning strings describing region drift or symlink targets left untouched.
    """
    from aim.core.models import Manifest

    assert isinstance(m, Manifest)

    applied = [repo_rules.render_rule(r) for r in m.rules]
    canonical_regions = _render_regions(
        m.instruction_template,
        applied,
        rules_mode=profile.rules_mode,
    )

    # A selected archetype supplies its own AGENTS.md prose (read at the pinned SHA);
    # only aim's dynamic `rules` region is merged into it. Without an archetype, the
    # built-in template's full region set is used as before.
    archetype_base: str | None = None
    if m.instruction_archetype is not None:
        from aim.core import archetypes as archetypes_mod

        installed_archetype = m.instruction_archetype
        archetype_base = archetypes_mod.read_base_body(
            installed_archetype.repo_alias,
            installed_archetype.current.sha,
            installed_archetype.source_path,
        )
        fresh_regions = (
            {"rules": canonical_regions["rules"]} if "rules" in canonical_regions else {}
        )
    else:
        fresh_regions = canonical_regions

    drift_warnings: list[str] = []
    agents_path = project_root / profile.agents_md

    if agents_path.exists() and not force:
        existing = agents_path.read_text()
        drift_warnings.extend(
            _detect_region_drift(agents_path.name, existing, m.managed_region_hashes)
        )
        merged = agents_md.merge(existing, fresh_regions)
    elif archetype_base is not None:
        merged = agents_md.merge(archetype_base, fresh_regions)
    else:
        merged = _render_for_template(
            m.instruction_template,
            applied,
            rules_mode=profile.rules_mode,
        )

    new_hashes = {r.name: hashing.hash_text(r.body) for r in agents_md.parse(merged)}

    symlink_paths: list[Path] = []
    for link_name in m.symlinks:
        target = project_root / link_name
        symlink_paths.append(target)
        if target.exists() and target.resolve() == agents_path.resolve():
            continue
        if target.exists() and not force:
            drift_warnings.append(f"{target.name} exists; left as-is (use --force to overwrite)")
            continue
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(agents_path.name)

    # Write AGENTS.md last so symlinks can reference it safely.
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    content_guard.assert_no_hidden_unicode(merged, source=agents_path.name)
    agents_path.write_text(merged)

    m.managed_region_hashes = new_hashes
    managed = [
        profile.agents_md,
        *(p.name for p in symlink_paths),
    ]
    m.managed_files = list(dict.fromkeys(managed))
    return drift_warnings
