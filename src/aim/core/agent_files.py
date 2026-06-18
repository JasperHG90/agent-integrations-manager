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
    return _AGENT_FROM_FILENAME.get(filename)


def _detect_region_drift(filename: str, body: str, stored_hashes: dict[str, str]) -> list[str]:
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
    """Render and write AGENTS.md and symlinks. Return drift warnings."""
    from aim.core.models import Manifest

    assert isinstance(m, Manifest)

    applied = [repo_rules.render_rule(r) for r in m.rules]
    fresh_regions_canonical = _render_regions(
        m.instruction_template,
        applied,
        rules_mode=profile.rules_mode,
    )

    drift_warnings: list[str] = []
    agents_path = project_root / profile.agents_md

    if agents_path.exists() and not force:
        existing = agents_path.read_text()
        drift_warnings.extend(
            _detect_region_drift(agents_path.name, existing, m.managed_region_hashes)
        )
        merged = agents_md.merge(existing, fresh_regions_canonical)
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
