"""`aim doctor` — audit drift across all configured roots.

Reports:
- skill content_hash drift (target dir edited)
- managed region hash drift (AGENTS.md / symlinks edited inside markers)
- skill target dir missing entirely
- snapshot dirs missing the `.complete` sentinel (cache health)
- registered repos that haven't been refreshed in `stale_repo_days` days
- orphan rule rows (DB row exists but body file is gone)

Exit-coded so it can run in CI: 0 = clean, 1 = drift / errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import select

from aim.core import (
    agents_md,
    db,
    hashing,
    manifest,
    mcp_registry,
    paths,
    repos,
    roots,
    rules,
)
from aim.core.install import _SNAPSHOT_SENTINEL
from aim.core.models import RegisteredRepo, RuleEntry


class DoctorPathEscapeError(ValueError):
    """A manifest-stored path resolves outside the project root."""


@dataclass
class Finding:
    severity: str  # "info" | "warning" | "error"
    project: Path | None
    message: str


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)
    projects_audited: int = 0

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def by_severity(self, sev: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == sev]


def _safe_project_path(root: Path, rel: str) -> Path | None:
    """Wrapper that catches unexpected resolution failures."""
    try:
        return paths.safe_project_path(root, rel)
    except (ValueError, OSError):
        return None


def audit(
    project_roots: list[Path] | None = None,
    *,
    stale_repo_days: int = 30,
) -> DoctorReport:
    report = DoctorReport()
    resolved_roots = project_roots if project_roots is not None else roots.list_roots()

    for root in resolved_roots:
        report.projects_audited += 1
        _audit_project(root, report)

    _audit_global_repos(report, stale_days=stale_repo_days)
    _audit_global_rules(report)
    _audit_snapshots(report)

    return report


def _audit_project(root: Path, report: DoctorReport) -> None:
    if not root.exists():
        report.findings.append(Finding("warning", root, f"project root does not exist: {root}"))
        return
    try:
        m = manifest.load(root)
    except manifest.ManifestNotFoundError:
        report.findings.append(Finding("warning", root, "no aim.lock.toml (not initialized)"))
        return

    # Region drift in AGENTS.md and symlinks.
    for managed in m.managed_files:
        target = _safe_project_path(root, managed)
        if target is None:
            report.findings.append(
                Finding(
                    "error",
                    root,
                    f"{managed}: manifest path escapes project root",
                )
            )
            continue
        if not target.exists():
            report.findings.append(
                Finding(
                    "error",
                    root,
                    f"{managed}: declared in manifest but missing on disk",
                )
            )
            continue
        try:
            regions = agents_md.parse(target.read_text())
        except agents_md.RegionError as exc:
            report.findings.append(Finding("error", root, f"{managed}: malformed markers — {exc}"))
            continue
        for region in regions:
            prior = m.managed_region_hashes.get(region.name)
            if prior is None:
                continue
            if hashing.hash_text(region.body) != prior:
                report.findings.append(
                    Finding(
                        "warning",
                        root,
                        f"{managed}: region {region.name!r} edited since last write",
                    )
                )

    # Skill drift.
    for skill in m.skills:
        target = _safe_project_path(root, skill.target_dir)
        if target is None:
            report.findings.append(
                Finding(
                    "error",
                    root,
                    f"{skill.qualified_name}: target {skill.target_dir} escapes project root",
                )
            )
            continue
        if not target.exists():
            report.findings.append(
                Finding("error", root, f"{skill.qualified_name}: target {skill.target_dir} missing")
            )
            continue
        if skill.content_hash is None:
            report.findings.append(
                Finding("info", root, f"{skill.qualified_name}: no content_hash (legacy install)")
            )
            continue
        current = hashing.hash_tree(target)
        if current != skill.content_hash:
            report.findings.append(
                Finding(
                    "warning",
                    root,
                    f"{skill.qualified_name}: {skill.target_dir} edited since install",
                )
            )

    # Agent drift.
    for agent in m.agents:
        target = _safe_project_path(root, agent.target_path)
        if target is None:
            report.findings.append(
                Finding(
                    "error",
                    root,
                    f"{agent.qualified_name}: target {agent.target_path} escapes project root",
                )
            )
            continue
        if not target.exists():
            report.findings.append(
                Finding(
                    "error", root, f"{agent.qualified_name}: target {agent.target_path} missing"
                )
            )
            continue
        if agent.content_hash is None:
            report.findings.append(
                Finding("info", root, f"{agent.qualified_name}: no content_hash (legacy install)")
            )
            continue
        current = hashing.hash_text(target.read_text(encoding="utf-8"))
        if current != agent.content_hash:
            report.findings.append(
                Finding(
                    "warning",
                    root,
                    f"{agent.qualified_name}: {agent.target_path} edited since install",
                )
            )

    # MCP server drift.
    try:
        mcp_data = mcp_registry.read_mcp_json(root)
    except mcp_registry.McpRegistryError as exc:
        report.findings.append(Finding("error", root, f"invalid .mcp.json: {exc}"))
        mcp_data = {"mcpServers": {}}
    mcp_servers = mcp_data.get("mcpServers", {})
    for mcp in m.mcp_servers:
        if not isinstance(mcp_servers, dict) or mcp.alias not in mcp_servers:
            report.findings.append(
                Finding("error", root, f"MCP alias {mcp.alias!r}: missing from .mcp.json")
            )
            continue
        current_hash = hashing.hash_text(mcp_registry._canonical_json(mcp_servers[mcp.alias]))
        if current_hash != mcp.entry_hash:
            report.findings.append(
                Finding(
                    "warning",
                    root,
                    f"MCP alias {mcp.alias!r}: .mcp.json entry edited since install",
                )
            )


def _audit_global_repos(report: DoctorReport, *, stale_days: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    with db.session() as session:
        repo_rows: list[RegisteredRepo] = list(session.exec(select(RegisteredRepo)).all())
    for repo in repo_rows:
        fetched = repo.last_fetched_at
        if fetched is None:
            continue
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        if fetched < cutoff:
            age_days = (datetime.now(UTC) - fetched).days
            report.findings.append(
                Finding(
                    "info",
                    None,
                    f"repo {repo.alias}: not refreshed in {age_days} days (last {fetched.isoformat()})",
                )
            )
        clone_dir = repos.clone_dir(repo.alias)
        if not clone_dir.exists():
            report.findings.append(
                Finding(
                    "warning",
                    None,
                    f"repo {repo.alias}: cache clone missing at {clone_dir}",
                )
            )


def _audit_global_rules(report: DoctorReport) -> None:
    with db.session() as session:
        rule_rows: list[RuleEntry] = list(session.exec(select(RuleEntry)).all())
    for entry in rule_rows:
        body_path = rules.body_path(entry.name)
        if not body_path.exists():
            report.findings.append(
                Finding(
                    "warning",
                    None,
                    f"rule {entry.name}: DB row exists but body file missing at {body_path}",
                )
            )


def _audit_snapshots(report: DoctorReport) -> None:
    root = paths.snapshots_cache_dir()
    if not root.exists():
        return
    # Each leaf dir is repo_alias/sha/skill_name. Walk three levels and check
    # for the sentinel.
    for repo_dir in root.iterdir():
        if not repo_dir.is_dir():
            continue
        for sha_dir in repo_dir.iterdir():
            if not sha_dir.is_dir():
                continue
            for skill_dir in sha_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                if not (skill_dir / _SNAPSHOT_SENTINEL).exists():
                    report.findings.append(
                        Finding(
                            "warning",
                            None,
                            f"snapshot {repo_dir.name}/{sha_dir.name}/{skill_dir.name}: "
                            "missing .aim.complete sentinel",
                        )
                    )
