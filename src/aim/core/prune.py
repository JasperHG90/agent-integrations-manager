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

from aim.core import declarations, layout_profiles, manifest, mcp_registry


class PruneError(RuntimeError):
    pass


@dataclass
class PruneOptions:
    project_root: Path
    dry_run: bool = False
    force: bool = False
    layout_profile: str | None = None
    excludes: list[str] = field(default_factory=list)


@dataclass
class PruneItem:
    kind: str  # "skill" | "agent" | "rule" | "mcp" | "symlink"
    path: str  # relative path, MCP alias, or "<inline>/<name>" for inline rules
    action: str  # "removed" | "would-remove" | "removed-stale-entry" | "skipped" | "kept" | "skipped-unsafe" | "error: ..."


@dataclass
class PruneResult:
    removed: list[PruneItem] = field(default_factory=list)
    kept: list[PruneItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------- helpers ----------


def _ensure_inside(root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError):
        return False
    return resolved == root_resolved or resolved.is_relative_to(root_resolved)


def _load_aimignore(project_root: Path) -> list[str]:
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
    """Match a relative path or MCP alias against glob patterns."""
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
    if profile.rules_mode == "inline":
        return f"<inline>/{rule_name}"
    return f"{profile.rules_dir}/{rule_name}.md"


def _rule_name_from_rel(rel: str) -> str:
    if rel.startswith("<inline>/"):
        return rel[len("<inline>/") :]
    return Path(rel).stem


def _resolve_profile(
    project_root: Path, options: PruneOptions, decl_profile: str | None
) -> layout_profiles.LayoutProfile:
    active = options.layout_profile or decl_profile
    if active:
        try:
            return layout_profiles.get_profile(project_root, active)
        except layout_profiles.LayoutProfileNotFoundError:
            return layout_profiles.BUILTIN_CLAUDE
    return layout_profiles.BUILTIN_CLAUDE


# ---------- drift computation ----------


def _drift(
    m: manifest.Manifest,
    decl: declarations.ProjectDeclarations,
    profile: layout_profiles.LayoutProfile,
    exclude_patterns: list[str],
) -> tuple[list[PruneItem], list[PruneItem], list[str]]:
    """Compute drift candidates. Returns (candidates, kept, warnings).

    candidates: lockfile entries not in declarations (action: would-remove or skipped)
    kept: declared + installed items (informational; for verbose output)
    warnings: .aimignore patterns that matched neither a candidate nor a kept item
    """
    candidates: list[PruneItem] = []
    kept: list[PruneItem] = []

    declared_skill_qnames = {s.qualified_name for s in decl.skills}
    declared_agent_qnames = {a.qualified_name for a in decl.agents}
    declared_mcp_aliases = {mc.alias for mc in decl.mcp_servers}
    declared_rule_qnames = {r.qualified_name for r in decl.rules}
    declared_symlinks = set(decl.symlinks)

    # Patterns that matched at least one drift candidate OR kept item.
    # Patterns matching only kept items are not "obsolete" — the user may have
    # added them preemptively to protect against future drift.
    matched_patterns: set[str] = set()

    def _record_pattern_match(rel: str) -> None:
        matching = _matching_pattern(rel, exclude_patterns)
        if matching is not None:
            matched_patterns.add(matching)

    def _maybe(candidate_kind: str, rel: str, declared: bool) -> None:
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

    warnings = _obsolete_pattern_warnings(exclude_patterns, matched_patterns)
    return candidates, kept, warnings


def _matching_pattern(rel: str, patterns: list[str]) -> str | None:
    """Return the first pattern that matches rel, or None."""
    for raw in patterns:
        if _is_excluded(rel, [raw]):
            return raw
    return None


def _obsolete_pattern_warnings(patterns: list[str], matched: set[str]) -> list[str]:
    """Warn about .aimignore patterns that matched neither a drift candidate
    nor a kept item — these are likely dead config under the new prune model."""
    warnings: list[str] = []
    for raw in patterns:
        if raw in matched:
            continue
        warnings.append(
            f".aimignore pattern {raw!r} matched no drift candidate or kept item; "
            "it may be obsolete under the new prune behavior."
        )
    return warnings


# ---------- load + validate ----------


def _load_state(
    project_root: Path,
) -> tuple[declarations.ProjectDeclarations, manifest.Manifest] | None:
    """Load declarations and manifest. Returns None if aim.toml is missing."""
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
    # aim.toml uses None to mean "default"; the lockfile stores the resolved name.
    decl_resolved = decl.layout_profile or layout_profiles.BUILTIN_CLAUDE.name
    if m.layout_profile != decl_resolved:
        raise PruneError(
            f"lockfile layout_profile={m.layout_profile!r} but aim.toml declares "
            f"{decl.layout_profile!r}; run `aim sync` first to reconcile"
        )


# ---------- plan / apply / run ----------


def plan(options: PruneOptions) -> PruneResult:
    """Compute the prune plan without applying it."""
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
    """Apply a plan. Re-validates state; only acts on items still in drift
    at apply time (TOCTOU safety)."""
    project_root = options.project_root.resolve()
    state = _load_state(project_root)
    if state is None:
        raise PruneError("aim.toml no longer exists; re-run `aim prune`")
    decl, m = state
    _check_layout_match(m, decl)
    profile = _resolve_profile(project_root, options, decl.layout_profile)
    exclude_patterns = _load_aimignore(project_root) + list(options.excludes)

    # Recompute current drift.
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

    # Track what to remove from the lockfile per kind.
    skill_paths = {p for k, p in to_apply_keys if k == "skill"}
    agent_paths = {p for k, p in to_apply_keys if k == "agent"}
    rule_rels = {p for k, p in to_apply_keys if k == "rule"}
    symlink_paths = {p for k, p in to_apply_keys if k == "symlink"}
    mcp_aliases = {p for k, p in to_apply_keys if k == "mcp"}

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
    """Backward-compat wrapper: plan then apply (no prompt).

    Used by programmatic callers (e.g. the TUI) that manage their own
    confirmation flow. When ``dry_run=True``, only the plan is returned.
    """
    plan_result = plan(options)
    if options.dry_run:
        return plan_result
    if not any(c.action == "would-remove" for c in plan_result.removed):
        return plan_result
    return apply(options, plan_result)


# ---------- rendering ----------


_KIND_ORDER = ("skill", "agent", "rule", "symlink", "mcp")
_KIND_LABEL = {
    "skill": "Skills",
    "agent": "Agents",
    "rule": "Rules",
    "symlink": "Symlinks",
    "mcp": "MCP servers",
}


def render_prune_plan(result: PruneResult, *, verbose: bool = False) -> None:
    """Render the prune plan to stdout via rich.

    Default output focuses on removals. With ``verbose=True``, a "Kept"
    section is appended for visibility into what is managed but not touched.
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
