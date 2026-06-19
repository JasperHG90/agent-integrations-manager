"""Policy / governance.

A policy blacklists repos, blocks specific skills/agents/rules, restricts which
layout profiles are allowed, and configures risk scanning. There is one resolved
policy per project.

Sources & precedence: built-in permissive default < local policy (stored in the
global SQLite DB) < org policy (a git repo, added in a later phase). An org policy
replaces the local one — the org is the trust root.

The real enforcement boundary is the committed lockfile + review/CI (`aim policy
validate`): the lockfile pins the policy repo + hash, so swapping in a weaker policy
is a visible, checkable diff. The local client refusing on mismatch is early-warning
UX, not the security boundary.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from aim.core import db, git, paths
from aim.core.models import GlobalSetting

DEFAULT_MODEL_ID = "protectai/deberta-v3-base-prompt-injection-v2"

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


class PolicyViolationError(ValueError):
    """An action is disallowed by the active policy."""


class PolicyError(ValueError):
    """A policy document could not be parsed or is otherwise invalid."""


# ---------- models ----------


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: str = "high"
    prompt: str = ""  # preset rules keep their prompt in-code; custom rules carry their own


class RiskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: str = "warn"  # warn | block
    backend: str = "tiered"  # null | local | judge | tiered
    model_id: str = DEFAULT_MODEL_ID
    block_threshold: str = "high"  # low | medium | high
    escalate_threshold: str = "medium"
    judge: str | None = None
    load_custom: bool = True  # also evaluate custom rules from aim.rules.toml
    # preset rule id -> severity override (str) or False to disable.
    preset_overrides: dict[str, str | bool] = Field(default_factory=dict)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    name: str = "local"
    blocked_repos: list[str] = Field(default_factory=list)
    blocked_skills: list[str] = Field(default_factory=list)
    blocked_agents: list[str] = Field(default_factory=list)
    blocked_rules: list[str] = Field(default_factory=list)
    blocked_mcp: list[str] = Field(default_factory=list)  # by alias or registry_name
    allowed_profiles: list[str] = Field(default_factory=list)  # empty = all allowed
    risk: RiskSettings = Field(default_factory=RiskSettings)
    custom_rules: list[RiskRule] = Field(default_factory=list)


# Built-in preset rules: ids + default severities + the in-code prompt the judge uses.
PRESET_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        id="secret_exfiltration",
        severity="high",
        prompt="Flag if the artifact reads secrets, environment variables, or credential "
        "files (e.g. ~/.aws/credentials, .env, id_rsa) and sends them anywhere external.",
    ),
    RiskRule(
        id="destructive_ops",
        severity="high",
        prompt="Flag destructive operations: rm -rf, dropping a database, mass file "
        "deletion, or force-pushing/rewriting git history.",
    ),
    RiskRule(
        id="remote_code_exec",
        severity="high",
        prompt="Flag downloading and executing remote code: curl|sh, piping fetched "
        "content to a shell, or eval of downloaded code.",
    ),
    RiskRule(
        id="data_exfiltration",
        severity="high",
        prompt="Flag uploading or POSTing local data to non-allowlisted external hosts.",
    ),
    RiskRule(
        id="privilege_escalation",
        severity="medium",
        prompt="Flag privilege escalation: sudo, chmod/chown, or capability/permission changes.",
    ),
    RiskRule(
        id="disable_security",
        severity="high",
        prompt="Flag attempts to disable security controls: --no-verify, turning off "
        "scanning, or bypassing policy checks.",
    ),
    RiskRule(
        id="obfuscation",
        severity="medium",
        prompt="Flag obfuscation: base64-piped-to-shell, or hidden/encoded commands.",
    ),
)


def builtin_policy() -> Policy:
    """The permissive default used when no policy is configured."""
    return Policy(name="builtin")


def active_rules(policy: Policy) -> list[RiskRule]:
    """Resolve the rule set the judge should evaluate: presets (with policy
    overrides/disables applied) plus custom rules when enabled."""
    rules: list[RiskRule] = []
    for preset in PRESET_RULES:
        override = policy.risk.preset_overrides.get(preset.id, preset.severity)
        if override is False:
            continue
        severity = override if isinstance(override, str) else preset.severity
        rules.append(RiskRule(id=preset.id, severity=severity, prompt=preset.prompt))
    if policy.risk.load_custom:
        rules.extend(policy.custom_rules)
    return rules


# ---------- repo url normalization (relocated from cli for shared use) ----------


def normalize_repo_url(url: str) -> str:
    """Canonicalize a git URL for equality comparison: drop a trailing `.git`,
    rewrite `git@host:path` to `https://host/path`, and lowercase."""
    u = url.strip()
    if u.startswith("git@") and ":" in u:
        host, _, path = u[len("git@") :].partition(":")
        u = f"https://{host}/{path}"
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/").lower()


# ---------- (de)serialization ----------


def from_toml(text: str) -> Policy:
    """Parse a `policy.toml` document into a Policy."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"invalid policy.toml: {exc}") from exc

    repos_t = data.get("repos", {}) or {}
    artifacts_t = data.get("artifacts", {}) or {}
    profiles_t = data.get("profiles", {}) or {}
    risk_t = dict(data.get("risk", {}) or {})
    rules_t = dict(risk_t.pop("rules", {}) or {})

    preset_overrides = {k: v for k, v in rules_t.items() if k != "custom"}
    risk = RiskSettings(
        enabled=bool(risk_t.get("enabled", False)),
        mode=str(risk_t.get("mode", "warn")),
        backend=str(risk_t.get("backend", "tiered")),
        model_id=str(risk_t.get("model_id", DEFAULT_MODEL_ID)),
        block_threshold=str(risk_t.get("block_threshold", "high")),
        escalate_threshold=str(risk_t.get("escalate_threshold", "medium")),
        judge=risk_t.get("judge"),
        load_custom=bool(rules_t.get("custom", True)),
        preset_overrides=preset_overrides,
    )
    return Policy(
        version=int(data.get("version", 1)),
        name=str(data.get("name", "local")),
        blocked_repos=list(repos_t.get("blocked", [])),
        blocked_skills=list(artifacts_t.get("blocked_skills", [])),
        blocked_agents=list(artifacts_t.get("blocked_agents", [])),
        blocked_rules=list(artifacts_t.get("blocked_rules", [])),
        blocked_mcp=list(artifacts_t.get("blocked_mcp", [])),
        allowed_profiles=list(profiles_t.get("allowed", [])),
        risk=risk,
    )


def to_toml(policy: Policy) -> str:
    """Serialize a Policy to a `policy.toml` document (custom rules excluded —
    those live in aim.rules.toml)."""
    doc: dict = {"version": policy.version, "name": policy.name}
    if policy.blocked_repos:
        doc["repos"] = {"blocked": policy.blocked_repos}
    artifacts: dict = {}
    if policy.blocked_skills:
        artifacts["blocked_skills"] = policy.blocked_skills
    if policy.blocked_agents:
        artifacts["blocked_agents"] = policy.blocked_agents
    if policy.blocked_rules:
        artifacts["blocked_rules"] = policy.blocked_rules
    if policy.blocked_mcp:
        artifacts["blocked_mcp"] = policy.blocked_mcp
    if artifacts:
        doc["artifacts"] = artifacts
    if policy.allowed_profiles:
        doc["profiles"] = {"allowed": policy.allowed_profiles}
    risk: dict = {
        "enabled": policy.risk.enabled,
        "mode": policy.risk.mode,
        "backend": policy.risk.backend,
        "model_id": policy.risk.model_id,
        "block_threshold": policy.risk.block_threshold,
        "escalate_threshold": policy.risk.escalate_threshold,
    }
    if policy.risk.judge is not None:
        risk["judge"] = policy.risk.judge
    rules: dict = dict(policy.risk.preset_overrides)
    rules["custom"] = policy.risk.load_custom
    risk["rules"] = rules
    doc["risk"] = risk
    return tomli_w.dumps(doc)


def parse_rules_toml(text: str) -> list[RiskRule]:
    """Parse custom rules from an `aim.rules.toml` document."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"invalid aim.rules.toml: {exc}") from exc
    out: list[RiskRule] = []
    for raw in data.get("rule", []):
        try:
            out.append(RiskRule(**raw))
        except Exception as exc:
            raise PolicyError(f"invalid rule in aim.rules.toml: {exc}") from exc
    return out


def render_rules_toml(rules: list[RiskRule]) -> str:
    """Serialize custom rules to an `aim.rules.toml` document."""
    return tomli_w.dumps({"rule": [r.model_dump() for r in rules]})


def compute_hash(policy: Policy) -> str:
    """Deterministic content hash over the fully-resolved policy (fields + risk +
    custom rules). Used to pin the policy in the lockfile and detect drift/tampering.

    Custom rules are canonicalized by `id` so re-importing the same rules in a
    different order does not change the hash."""
    data = policy.model_dump(mode="json")
    data["custom_rules"] = sorted(data.get("custom_rules", []), key=lambda r: r["id"])
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------- local policy storage (global SQLite DB, GlobalSetting-style) ----------

_LOCAL_POLICY_KEY = "policy:local"
_BINDING_KEY = "policy:binding"
_ORG_SNAPSHOT_KEY = "policy:org_snapshot"


def load_local_policy() -> Policy | None:
    with db.session() as session:
        row = session.get(GlobalSetting, _LOCAL_POLICY_KEY)
    if row is None:
        return None
    return Policy.model_validate_json(row.value)


def save_local_policy(policy: Policy) -> None:
    blob = policy.model_dump_json()
    with db.session() as session:
        row = session.get(GlobalSetting, _LOCAL_POLICY_KEY)
        if row is None:
            session.add(GlobalSetting(key=_LOCAL_POLICY_KEY, value=blob))
        else:
            row.value = blob
        session.commit()


def clear_local_policy() -> None:
    with db.session() as session:
        row = session.get(GlobalSetting, _LOCAL_POLICY_KEY)
        if row is not None:
            session.delete(row)
            session.commit()


def get_binding() -> dict | None:
    """The optional org-policy binding pointer: {'repo': url, 'ref': ref} or None."""
    with db.session() as session:
        row = session.get(GlobalSetting, _BINDING_KEY)
    return json.loads(row.value) if row is not None else None


def set_binding(repo: str, ref: str = "HEAD") -> None:
    blob = json.dumps({"repo": repo, "ref": ref})
    with db.session() as session:
        row = session.get(GlobalSetting, _BINDING_KEY)
        if row is None:
            session.add(GlobalSetting(key=_BINDING_KEY, value=blob))
        else:
            row.value = blob
        session.commit()


def clear_binding() -> None:
    with db.session() as session:
        row = session.get(GlobalSetting, _BINDING_KEY)
        if row is not None:
            session.delete(row)
            session.commit()


# ---------- org policy repo (fetched, pinned, replaces local when bound) ----------


def _policy_clone_dir(repo_url: str) -> Path:
    key = hashlib.sha256(normalize_repo_url(repo_url).encode("utf-8")).hexdigest()[:16]
    return paths.user_cache_dir() / "policy" / key


def fetch_org_policy(repo_url: str, ref: str = "HEAD") -> tuple[Policy, str]:
    """Bare-clone/fetch the policy repo and read `policy.toml` (+ optional
    `aim.rules.toml`) at the resolved commit. Returns (Policy, sha). Network op:
    call only from bind/refresh/validate, never from the lock/deploy hot path."""
    paths.ensure_global_dirs()
    dest = _policy_clone_dir(repo_url)
    backend = git.get_backend()
    if dest.exists():
        backend.fetch(dest)
    else:
        backend.clone_bare(repo_url, dest)
    sha = backend.resolve_ref(dest, ref)
    pol = from_toml(backend.cat_file(dest, sha, "policy.toml"))
    try:
        pol.custom_rules = parse_rules_toml(backend.cat_file(dest, sha, "aim.rules.toml"))
    except git.GitError:
        pass  # aim.rules.toml is optional
    return pol, sha


def _save_org_snapshot(repo_url: str, ref: str, sha: str, pol: Policy) -> None:
    blob = json.dumps(
        {
            "repo": repo_url,
            "ref": ref,
            "sha": sha,
            "hash": compute_hash(pol),
            "policy": pol.model_dump(mode="json"),
        }
    )
    with db.session() as session:
        row = session.get(GlobalSetting, _ORG_SNAPSHOT_KEY)
        if row is None:
            session.add(GlobalSetting(key=_ORG_SNAPSHOT_KEY, value=blob))
        else:
            row.value = blob
        session.commit()


def _clear_org_snapshot() -> None:
    with db.session() as session:
        row = session.get(GlobalSetting, _ORG_SNAPSHOT_KEY)
        if row is not None:
            session.delete(row)
            session.commit()


def load_org_snapshot() -> ResolvedPolicy | None:
    """The last-fetched org policy, read from the DB (no network). Used by
    resolution so `lock` stays offline."""
    with db.session() as session:
        row = session.get(GlobalSetting, _ORG_SNAPSHOT_KEY)
    if row is None:
        return None
    data = json.loads(row.value)
    pol = Policy.model_validate(data["policy"])
    return ResolvedPolicy(pol, "org", data["repo"], data["hash"])


def bind(repo_url: str, ref: str = "HEAD") -> ResolvedPolicy:
    """Bind to an org policy repo: fetch it, pin a snapshot, and record the
    binding. The org policy then replaces the local one for this machine."""
    pol, sha = fetch_org_policy(repo_url, ref)
    set_binding(repo_url, ref)
    _save_org_snapshot(repo_url, ref, sha, pol)
    return ResolvedPolicy(pol, "org", repo_url, compute_hash(pol))


def refresh_org_policy() -> ResolvedPolicy | None:
    """Re-fetch the bound org policy and update the cached snapshot."""
    binding = get_binding()
    if binding is None:
        return None
    return bind(binding["repo"], binding.get("ref", "HEAD"))


def unbind() -> None:
    clear_binding()
    _clear_org_snapshot()


# ---------- resolution ----------


@dataclass(frozen=True)
class ResolvedPolicy:
    policy: Policy
    source: str  # "builtin" | "local" | "org"
    repo: str | None  # org policy repo url, else None
    hash: str | None  # org policy hash, or local snapshot hash, else None


def resolve_effective(project_root: Path | None = None) -> ResolvedPolicy:
    """Resolve the effective policy: built-in default < local (DB) < org (bound).
    A bound org policy replaces local. Offline: reads the cached org snapshot,
    never fetches (bind/refresh do that)."""
    if get_binding() is not None:
        snapshot = load_org_snapshot()
        if snapshot is not None:
            return snapshot
    local = load_local_policy()
    if local is not None:
        return ResolvedPolicy(local, "local", None, compute_hash(local))
    return ResolvedPolicy(builtin_policy(), "builtin", None, None)


def effective_policy(project_root: Path | None = None) -> Policy:
    """The resolved policy without computing a hash — the cheap path used by the
    per-artifact deploy gates (which only need the policy, not its hash)."""
    if get_binding() is not None:
        snapshot = load_org_snapshot()
        if snapshot is not None:
            return snapshot.policy
    return load_local_policy() or builtin_policy()


# ---------- enforcement predicates (mirror content_guard.require_secure_url) ----------


def assert_repo_allowed(policy: Policy, alias: str, url: str) -> None:
    if not policy.blocked_repos:
        return
    norm = normalize_repo_url(url)
    for entry in policy.blocked_repos:
        if entry == alias or normalize_repo_url(entry) == norm:
            raise PolicyViolationError(
                f"repo {alias!r} ({url}) is blocked by policy {policy.name!r}"
            )


def assert_artifact_allowed(policy: Policy, kind: str, qualified_name: str) -> None:
    blocked = {
        "skill": policy.blocked_skills,
        "agent": policy.blocked_agents,
        "rule": policy.blocked_rules,
    }.get(kind, [])
    if qualified_name in blocked:
        raise PolicyViolationError(
            f"{kind} {qualified_name!r} is blocked by policy {policy.name!r}"
        )


def assert_mcp_allowed(policy: Policy, alias: str, registry_name: str) -> None:
    if not policy.blocked_mcp:
        return
    if alias in policy.blocked_mcp or registry_name in policy.blocked_mcp:
        raise PolicyViolationError(
            f"MCP server {alias!r} ({registry_name}) is blocked by policy {policy.name!r}"
        )


def assert_profile_allowed(policy: Policy, layout_profile_name: str | None) -> None:
    if not policy.allowed_profiles or layout_profile_name is None:
        return
    if layout_profile_name not in policy.allowed_profiles:
        raise PolicyViolationError(
            f"layout profile {layout_profile_name!r} is not in the policy "
            f"allow-list {policy.allowed_profiles} (policy {policy.name!r})"
        )
