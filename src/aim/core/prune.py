"""`aim prune` — remove lockfile entries no longer declared in `aim.toml`.

Prune compares the user's declarations (`aim.toml`) against the installed
state (`aim.lock.toml`) and removes lockfile entries — and their on-disk
files — that are no longer declared. This is the inverse of `aim sync`,
which installs missing declarations. Together the two commands reconcile
a project to its declared state.

Prune does **not** scan the managed directories for files not in the
lockfile. Files installed by external tools (Terraform, plugins, a
teammate's local skill) are left alone — aim only touches what it
manages.

By default, prune shows a plan and prompts for confirmation before
applying. Use `--force`/`--skip-plan` to apply immediately, or
`--dry-run`/`-n` to show the plan and exit without prompting.

Drift candidates can be protected from removal via `.aimignore` patterns
(one per line) at the project root, e.g.::

    .claude/skills/legacy-tool
    mcp:terraform-provisioner

Blank lines and ``#`` comments are ignored.
"""

from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from aim.core import (
    declarations,
    layout_profiles,
    manifest,
    mcp_registry,
    plugin_kinds,
)


class PruneError(RuntimeError):
    """Raise when a prune operation cannot proceed safely."""


@dataclass
class PruneOptions:
    """Hold the inputs that configure a single prune invocation."""

    project_root: Path
    dry_run: bool = False
    force: bool = False
    layout_profile: str | None = None
    excludes: list[str] = field(default_factory=list)


@dataclass
class PruneItem:
    """Describe one prune candidate and the action taken or planned for it."""

    kind: str  # "skill" | "agent" | "rule" | "mcp" | "symlink" | "plugin"
    path: str  # relative path, MCP alias, or "<inline>/<name>" for inline rules
    action: str  # "removed" | "would-remove" | "removed-stale-entry" | "skipped" | "kept" | "skipped-unsafe" | "error: ..."


@dataclass
class PruneResult:
    """Hold the outcome of a prune plan or apply: removed, kept, and warnings."""

    removed: list[PruneItem] = field(default_factory=list)
    kept: list[PruneItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _ensure_inside(root: Path, path: Path) -> bool:
    """Return whether ``path`` resolves to a location at or under ``root``.

    Args:
        root: The project root that deletions must stay within.
        path: The candidate path to validate before removal.

    Returns:
        True if ``path`` is ``root`` or lies inside it; False on error or escape.
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError):
        return False
    return resolved == root_resolved or resolved.is_relative_to(root_resolved)


def _load_aimignore(project_root: Path) -> list[str]:
    """Read ``.aimignore`` patterns from the project root.

    Args:
        project_root: Directory holding the optional ``.aimignore`` file.

    Returns:
        The non-blank, non-comment patterns; an empty list if the file is
        missing or unreadable.
    """
    ignore_path = project_root / ".aimignore"
    if not ignore_path.is_file():
        return []
    try:
        text = ignore_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    patterns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _is_excluded(rel: str, patterns: list[str]) -> bool:
    """Return whether a relative path or MCP alias matches any glob pattern.

    Args:
        rel: A relative path or MCP alias to test.
        patterns: ``.aimignore``/``--exclude`` glob patterns; ``mcp:`` prefixes
            match aliases.

    Returns:
        True if any pattern matches ``rel``.
    """
    rel_dir = rel + "/"
    for raw in patterns:
        if raw.startswith("mcp:"):
            alias_pattern = raw[4:]
            if fnmatch.fnmatch(rel, alias_pattern):
                return True
            continue
        if fnmatch.fnmatch(rel, raw.rstrip("/")):
            return True
        prefix = raw.rstrip("/")
        if prefix.endswith("/*"):
            if rel_dir.startswith(prefix[:-1]):
                return True
        elif not prefix.endswith("*"):
            if rel_dir.startswith(prefix + "/"):
                return True
    return False


def _rule_rel(profile: layout_profiles.LayoutProfile, rule_name: str) -> str:
    """Build the relative path or inline key identifying a rule under a profile.

    Args:
        profile: The layout profile determining inline vs. file-backed rules.
        rule_name: The unqualified rule name.

    Returns:
        An ``<inline>/<name>`` key for inline profiles, otherwise a ``.md`` path.
    """
    if profile.rules_mode == "inline":
        return f"<inline>/{rule_name}"
    return f"{profile.rules_dir}/{rule_name}.md"


def _rule_name_from_rel(rel: str) -> str:
    """Recover the unqualified rule name from its relative path or inline key.

    Args:
        rel: An ``<inline>/<name>`` key or a file path produced by ``_rule_rel``.

    Returns:
        The bare rule name.
    """
    if rel.startswith("<inline>/"):
        return rel[len("<inline>/") :]
    return Path(rel).stem


def _resolve_profile(
    project_root: Path, options: PruneOptions, decl_profile: str | None
) -> layout_profiles.LayoutProfile:
    """Resolve the active layout profile, falling back to the built-in Claude one.

    Args:
        project_root: Root used to look up custom profiles.
        options: Prune options whose ``layout_profile`` takes precedence.
        decl_profile: Profile declared in ``aim.toml``, if any.

    Returns:
        The resolved profile, or the built-in Claude profile when none is set or
        the named profile is not found.
    """
    active = options.layout_profile or decl_profile
    if active:
        try:
            return layout_profiles.get_profile(project_root, active)
        except layout_profiles.LayoutProfileNotFoundError:
            return layout_profiles.BUILTIN_CLAUDE
    return layout_profiles.BUILTIN_CLAUDE


def _drift(
    m: manifest.Manifest,
    decl: declarations.ProjectDeclarations,
    profile: layout_profiles.LayoutProfile,
    exclude_patterns: list[str],
) -> tuple[list[PruneItem], list[PruneItem], list[str]]:
    """Compare the lockfile against declarations to compute drift.

    Args:
        m: The loaded manifest (installed state).
        decl: The project declarations (desired state).
        profile: The active layout profile.
        exclude_patterns: Patterns protecting candidates from removal.

    Returns:
        A ``(candidates, kept, warnings)`` tuple where candidates are lockfile
        entries not in declarations (``would-remove`` or ``skipped``), kept are
        declared and installed items, and warnings flag exclude patterns that
        matched neither a candidate nor a kept item.
    """
    candidates: list[PruneItem] = []
    kept: list[PruneItem] = []

    declared_skill_qnames = {s.qualified_name for s in decl.skills}
    declared_agent_qnames = {a.qualified_name for a in decl.agents}
    declared_mcp_aliases = {mc.alias for mc in decl.mcp_servers}
    declared_rule_qnames = {r.qualified_name for r in decl.rules}
    declared_plugin_qnames = {p.qualified_name for p in decl.plugins}
    declared_symlinks = set(decl.symlinks)

    # Patterns that matched at least one drift candidate OR kept item.
    # Patterns matching only kept items are not "obsolete" — the user may have
    # added them preemptively to protect against future drift.
    matched_patterns: set[str] = set()

    def _record_pattern_match(rel: str) -> None:
        """Record that ``rel`` was covered by an exclude pattern, if any."""
        matching = _matching_pattern(rel, exclude_patterns)
        if matching is not None:
            matched_patterns.add(matching)

    def _maybe(candidate_kind: str, rel: str, declared: bool) -> None:
        """Classify one installed item as kept, skipped, or would-remove."""
        if declared:
            kept.append(PruneItem(candidate_kind, rel, "kept"))
            _record_pattern_match(rel)
            return
        matching = _matching_pattern(rel, exclude_patterns)
        if matching is not None:
            candidates.append(PruneItem(candidate_kind, rel, "skipped"))
            matched_patterns.add(matching)
        else:
            candidates.append(PruneItem(candidate_kind, rel, "would-remove"))

    for s in m.skills:
        _maybe("skill", s.target_dir, s.qualified_name in declared_skill_qnames)
    for a in m.agents:
        _maybe("agent", a.target_path, a.qualified_name in declared_agent_qnames)
    for rule in m.rules:
        rule_name = rule.qualified_name.split("/", 1)[-1]
        _maybe("rule", _rule_rel(profile, rule_name), rule.qualified_name in declared_rule_qnames)
    for sym in m.symlinks:
        _maybe("symlink", sym, sym in declared_symlinks)
    for mc in m.mcp_servers:
        _maybe("mcp", mc.alias, mc.alias in declared_mcp_aliases)
    for plug in m.plugins:
        _maybe("plugin", plug.target_dir, plug.qualified_name in declared_plugin_qnames)

    warnings = _obsolete_pattern_warnings(exclude_patterns, matched_patterns)
    return candidates, kept, warnings


def _matching_pattern(rel: str, patterns: list[str]) -> str | None:
    """Return the first pattern that matches ``rel``, or None.

    Args:
        rel: A relative path or MCP alias.
        patterns: Candidate exclude patterns, in priority order.

    Returns:
        The first matching pattern, or None if none match.
    """
    for raw in patterns:
        if _is_excluded(rel, [raw]):
            return raw
    return None


def _obsolete_pattern_warnings(patterns: list[str], matched: set[str]) -> list[str]:
    """Build warnings for exclude patterns that matched nothing.

    Args:
        patterns: All exclude patterns supplied for the run.
        matched: Patterns that matched a drift candidate or kept item.

    Returns:
        One warning per unmatched pattern, flagging likely-obsolete config.
    """
    warnings: list[str] = []
    for raw in patterns:
        if raw in matched:
            continue
        warnings.append(
            f".aimignore pattern {raw!r} matched no drift candidate or kept item; "
            "it may be obsolete under the new prune behavior."
        )
    return warnings


def _load_state(
    project_root: Path,
) -> tuple[declarations.ProjectDeclarations, manifest.Manifest] | None:
    """Load declarations and manifest for a project.

    Args:
        project_root: The project root to load state from.

    Returns:
        A ``(declarations, manifest)`` tuple, or None if ``aim.toml`` is missing.

    Raises:
        PruneError: If ``aim.toml`` or ``aim.lock.toml`` is missing or unparsable.
    """
    try:
        decl = declarations.load(project_root)
    except declarations.DeclarationsNotFoundError:
        return None
    except Exception as exc:
        raise PruneError(f"failed to parse aim.toml: {exc}") from exc
    try:
        m = manifest.load(project_root)
    except manifest.ManifestNotFoundError as exc:
        raise PruneError(f"no aim.lock.toml in {project_root}; run `aim init` first") from exc
    except Exception as exc:
        raise PruneError(f"failed to parse aim.lock.toml: {exc}") from exc
    return decl, m


def _check_layout_match(m: manifest.Manifest, decl: declarations.ProjectDeclarations) -> None:
    """Verify the lockfile and declarations agree on the layout profile.

    Args:
        m: The loaded manifest.
        decl: The project declarations.

    Raises:
        PruneError: If the resolved layout profiles differ.
    """
    # aim.toml uses None to mean "default"; the lockfile stores the resolved name.
    decl_resolved = decl.layout_profile or layout_profiles.BUILTIN_CLAUDE.name
    if m.layout_profile != decl_resolved:
        raise PruneError(
            f"lockfile layout_profile={m.layout_profile!r} but aim.toml declares "
            f"{decl.layout_profile!r}; run `aim sync` first to reconcile"
        )


def plan(options: PruneOptions) -> PruneResult:
    """Compute the prune plan without applying it.

    Args:
        options: The prune configuration.

    Returns:
        A result whose ``removed`` holds drift candidates and whose ``warnings``
        explain when there is nothing to prune.
    """
    project_root = options.project_root.resolve()
    state = _load_state(project_root)
    if state is None:
        result = PruneResult()
        result.warnings.append("no aim.toml found; nothing to prune (run `aim sync` to reconcile)")
        return result
    decl, m = state
    _check_layout_match(m, decl)
    profile = _resolve_profile(project_root, options, decl.layout_profile)
    exclude_patterns = _load_aimignore(project_root) + list(options.excludes)
    candidates, kept, warnings = _drift(m, decl, profile, exclude_patterns)
    return PruneResult(removed=candidates, kept=kept, warnings=warnings)


def apply(options: PruneOptions, plan_result: PruneResult) -> PruneResult:
    """Apply a previously computed prune plan.

    Re-validates state and only acts on items still in drift at apply time
    (TOCTOU safety).

    Args:
        options: The prune configuration.
        plan_result: The plan whose confirmed ``would-remove`` items to apply.

    Returns:
        A result recording what was removed, kept, or warned about.

    Raises:
        PruneError: If ``aim.toml`` vanished or an ``.mcp.json``/lockfile I/O
            step fails.
    """
    project_root = options.project_root.resolve()
    state = _load_state(project_root)
    if state is None:
        raise PruneError("aim.toml no longer exists; re-run `aim prune`")
    decl, m = state
    _check_layout_match(m, decl)
    profile = _resolve_profile(project_root, options, decl.layout_profile)
    exclude_patterns = _load_aimignore(project_root) + list(options.excludes)

    current, _, _ = _drift(m, decl, profile, exclude_patterns)

    # Items in the original plan that the user confirmed.
    plan_keys = {(c.kind, c.path) for c in plan_result.removed if c.action == "would-remove"}
    # Items that are STILL drift candidates now.
    current_keys = {(c.kind, c.path) for c in current if c.action == "would-remove"}
    to_apply_keys = plan_keys & current_keys

    result = PruneResult()

    if not to_apply_keys:
        result.warnings.append("Plan is stale; re-run `aim prune`")
        return result

    skill_paths = {p for k, p in to_apply_keys if k == "skill"}
    agent_paths = {p for k, p in to_apply_keys if k == "agent"}
    rule_rels = {p for k, p in to_apply_keys if k == "rule"}
    symlink_paths = {p for k, p in to_apply_keys if k == "symlink"}
    mcp_aliases = {p for k, p in to_apply_keys if k == "mcp"}
    plugin_paths = {p for k, p in to_apply_keys if k == "plugin"}

    # --- Phase 1: read .mcp.json BEFORE any deletion (fail fast, no partial state). ---
    mcp_data: dict | None = None
    if mcp_aliases:
        mcp_path = project_root / profile.mcp_json
        if mcp_path.exists():
            try:
                mcp_data = mcp_registry.read_mcp_json(project_root)
            except Exception as exc:
                raise PruneError(f"failed to read {profile.mcp_json}: {exc}") from exc

    # --- Phase 2: delete on-disk files / dirs. ---
    for kind, path in sorted(to_apply_keys):
        if kind == "mcp":
            continue  # handled in phase 3
        if kind == "rule" and profile.rules_mode == "inline":
            result.removed.append(PruneItem(kind, path, "removed-stale-entry"))
            result.warnings.append(
                f"rule {path!r} removed from lockfile; AGENTS.md is stale until `aim sync`"
            )
            continue
        entry = project_root / path
        if entry.exists() or entry.is_symlink():
            if not _ensure_inside(project_root, entry):
                result.kept.append(PruneItem(kind, path, "skipped-unsafe"))
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                result.removed.append(PruneItem(kind, path, "removed"))
            except Exception as exc:  # pragma: no cover - defensive
                result.kept.append(PruneItem(kind, path, f"error: {exc}"))
        else:
            result.removed.append(PruneItem(kind, path, "removed-stale-entry"))

    # --- Phase 3: mutate + write .mcp.json. ---
    if mcp_aliases:
        if mcp_data is not None:
            servers = mcp_data.get("mcpServers", {})
            if isinstance(servers, dict):
                for alias in mcp_aliases:
                    if alias in servers:
                        del servers[alias]
                        result.removed.append(PruneItem("mcp", alias, "removed"))
                    else:
                        result.removed.append(PruneItem("mcp", alias, "removed-stale-entry"))
                try:
                    mcp_registry.write_mcp_json(project_root, mcp_data)
                except Exception as exc:
                    raise PruneError(f"failed to write {profile.mcp_json}: {exc}") from exc
        else:
            for alias in mcp_aliases:
                result.removed.append(PruneItem("mcp", alias, "removed-stale-entry"))

    # --- Phase 3b: plugin registration cleanup (claude settings.json + marketplace.json). ---
    # The vendored plugin dir/file was already removed by the generic phase-2 loop;
    # claude plugins additionally need their settings.json enablement + marketplace
    # manifest reconciled. m.plugins is filtered here (before regeneration) so the
    # rewritten marketplace lists only survivors.
    if plugin_paths:
        pruned_plugins = [p for p in m.plugins if p.target_dir in plugin_paths]
        m.plugins = [p for p in m.plugins if p.target_dir not in plugin_paths]
        for p in pruned_plugins:
            pkind = plugin_kinds.get_kind(p.flavor, project_root)
            if pkind is None:
                continue  # kind spec gone; vendored files already removed in phase 2
            try:
                # m already excludes the pruned plugins, so refcount/regeneration
                # in the kind sees only survivors.
                pkind.unregister(project_root, p, m)
            except Exception as exc:
                raise PruneError(
                    f"failed to update client config for {p.qualified_name}: {exc}"
                ) from exc

    # --- Phase 4: mutate + save the lockfile. ---
    rule_names_to_remove = {_rule_name_from_rel(rel) for rel in rule_rels}
    m.skills = [s for s in m.skills if s.target_dir not in skill_paths]
    m.agents = [a for a in m.agents if a.target_path not in agent_paths]
    m.rules = [r for r in m.rules if r.qualified_name.split("/", 1)[-1] not in rule_names_to_remove]
    m.symlinks = [s for s in m.symlinks if s not in symlink_paths]
    m.mcp_servers = [mc for mc in m.mcp_servers if mc.alias not in mcp_aliases]
    # Inline rules are rendered into AGENTS.md managed regions; removing a rule
    # invalidates those region hashes. Clear them so the next `aim sync` rewrites
    # without false-negative drift detection.
    if rule_rels and profile.rules_mode == "inline":
        m.managed_region_hashes = {}

    manifest.save(project_root, m)
    return result


def run(options: PruneOptions) -> PruneResult:
    """Plan then apply without prompting (backward-compat wrapper).

    Used by programmatic callers (e.g. the TUI) that manage their own
    confirmation flow. When ``dry_run=True``, only the plan is returned.

    Args:
        options: The prune configuration.

    Returns:
        The plan when ``dry_run`` is set or nothing would be removed; otherwise
        the apply result.
    """
    plan_result = plan(options)
    if options.dry_run:
        return plan_result
    if not any(c.action == "would-remove" for c in plan_result.removed):
        return plan_result
    return apply(options, plan_result)


_KIND_ORDER = ("skill", "agent", "rule", "symlink", "mcp", "plugin")
_KIND_LABEL = {
    "skill": "Skills",
    "agent": "Agents",
    "rule": "Rules",
    "symlink": "Symlinks",
    "mcp": "MCP servers",
    "plugin": "Plugins",
}


def render_prune_plan(result: PruneResult, *, verbose: bool = False) -> None:
    """Render the prune plan to stdout via rich.

    Default output focuses on removals. With ``verbose=True``, a "Kept"
    section is appended for visibility into what is managed but not touched.

    Args:
        result: The prune result to render.
        verbose: When True, also list kept (declared and installed) items.
    """
    from rich.console import Console
    from rich.text import Text

    console = Console()
    removals = [i for i in result.removed if i.action == "would-remove"]
    skipped = [i for i in result.removed if i.action == "skipped"]

    if not removals and not skipped:
        console.print("[bold green]Nothing to prune.[/bold green]")
        return

    n = len(removals)
    header = f"Prune plan — {n} item(s) will be removed" if n else "Prune plan — no removals"
    console.print(f"[bold]{header}[/bold]\n")

    by_kind: dict[str, list[PruneItem]] = {}
    for item in removals:
        by_kind.setdefault(item.kind, []).append(item)

    for kind in _KIND_ORDER:
        items = by_kind.get(kind)
        if not items:
            continue
        console.print(f"  [bold]{_KIND_LABEL.get(kind, kind)}[/bold]")
        for item in sorted(items, key=lambda i: i.path):
            line = Text()
            line.append("    ")
            line.append("✗ ", style="bold red")
            line.append(item.path)
            line.append("  (not declared in aim.toml)", style="dim")
            console.print(line)
        console.print()

    if skipped:
        console.print("  [bold]Skipped (protected by .aimignore / --exclude)[/bold]")
        for item in sorted(skipped, key=lambda i: (i.kind, i.path)):
            console.print(f"    [yellow]•[/yellow] {item.kind} {item.path}")
        console.print()

    if verbose and result.kept:
        console.print("  [bold]Kept (declared + installed)[/bold]")
        kept_by_kind: dict[str, list[PruneItem]] = {}
        for item in result.kept:
            kept_by_kind.setdefault(item.kind, []).append(item)
        for kind in _KIND_ORDER:
            items = kept_by_kind.get(kind)
            if not items:
                continue
            for item in sorted(items, key=lambda i: i.path):
                console.print(f"    [green]✓[/green] {item.kind} {item.path}")
        console.print()

    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")
