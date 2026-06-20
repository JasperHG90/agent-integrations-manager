"""Policy / governance.

A policy blacklists repos, blocks specific skills/agents/rules, restricts which
layout profiles are allowed, and configures risk scanning. There is one resolved
policy per project.

The policy lives in the project's `aim.toml` `[policy]` table:
- `scope = "local"` — the policy is declared inline (repos/artifacts/profiles/risk
  + custom rules) and travels with the project.
- `scope = "org"`  — `repo`/`ref` point at an org policy git repo containing a
  self-contained `policy.toml` (the same sections as the inline `[policy]` table,
  custom `[[rule]]` entries included). The fetched policy is cached in the global DB
  (a cache only) so resolution stays offline; a missing/corrupt snapshot fails CLOSED
  rather than downgrading to permissive.

The real enforcement boundary is the committed lockfile + review/CI (`aim policy
validate`): `aim lock` pins the policy repo + commit SHA + content hash, so swapping
in a weaker policy is a visible, checkable diff. The local client is early-warning UX,
not the security boundary.
"""

from __future__ import annotations

import hashlib
import json
import threading
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from aim.core import content_guard, db, git, paths
from aim.core.models import GlobalSetting

DEFAULT_MODEL_ID = "protectai/deberta-v3-base-prompt-injection-v2"

# Cached org policy snapshots are refreshed at most once per process, and only
# when older than this TTL — so resolution stays offline day-to-day but a bound
# org policy still picks up upstream changes within a day.
_ORG_TTL = timedelta(hours=24)


class PolicyViolationError(ValueError):
    """An action is disallowed by the active policy."""


class PolicyError(ValueError):
    """A policy document could not be parsed or is otherwise invalid."""


class RiskRule(BaseModel):
    """A single risk rule the judge evaluates, with an id, severity, and prompt."""

    model_config = ConfigDict(extra="forbid")

    id: str
    severity: str = "high"
    prompt: str = ""  # preset rules keep their prompt in-code; custom rules carry their own


class RiskSettings(BaseModel):
    """Configure risk scanning: which screens run, thresholds, and override rules."""

    model_config = ConfigDict(extra="forbid")

    # Risk scanning is active iff `classifier` or `llm_judge` is on (no separate
    # `enabled` flag). When active it BLOCKS by default.
    mode: str = "block"  # warn | block
    classifier: bool = False  # local ONNX injection/jailbreak screen
    llm_judge: bool = False  # DSPy LLM judge against the rule set (both on -> screen gates judge)
    model_id: str = DEFAULT_MODEL_ID
    block_threshold: str = "high"  # low | medium | high
    # When both screens are on, a local verdict at/above this level blocks immediately
    # and the judge is skipped; a verdict below it falls through to the judge.
    escalate_threshold: str = "medium"
    judge: str | None = None
    load_custom: bool = True  # also evaluate the policy's custom [[rule]] entries
    # When False, a `--override-risk` override is refused — an org can make blocks
    # non-bypassable. Default True (the developer can override their own policy).
    allow_override: bool = True
    # preset rule id -> severity override (str) or False to disable.
    preset_overrides: dict[str, str | bool] = Field(default_factory=dict)


class Policy(BaseModel):
    """The resolved governance policy: blocklists, allowed profiles, and risk settings."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    name: str = "local"
    blocked_repos: list[str] = Field(default_factory=list)
    blocked_skills: list[str] = Field(default_factory=list)
    blocked_agents: list[str] = Field(default_factory=list)
    blocked_rules: list[str] = Field(default_factory=list)
    blocked_mcp: list[str] = Field(default_factory=list)  # by alias or registry_name
    allowed_profiles: list[str] = Field(default_factory=list)  # empty = all allowed
    # Allow-list of selectable instruction archetypes (by qualified name). Empty = all
    # allowed; non-empty constrains `archetype use` to the listed archetypes.
    allowed_archetypes: list[str] = Field(default_factory=list)
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
    """Return the permissive default used when no policy is configured."""
    return Policy(name="builtin")


def active_rules(policy: Policy) -> list[RiskRule]:
    """Resolve the rule set the judge should evaluate.

    Applies preset overrides/disables and appends custom rules when enabled.

    Args:
        policy: The resolved policy whose risk settings drive rule selection.

    Returns:
        The presets (after overrides) followed by enabled custom rules.
    """
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


def normalize_repo_url(url: str) -> str:
    """Canonicalize a git URL for equality comparison.

    Drops a trailing `.git`, rewrites `git@host:path` to `https://host/path`,
    and lowercases the result.

    Args:
        url: The git URL to canonicalize.

    Returns:
        The normalized URL.
    """
    u = url.strip()
    if u.startswith("git@") and ":" in u:
        host, _, path = u[len("git@") :].partition(":")
        u = f"https://{host}/{path}"
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/").lower()


def from_mapping(data: dict) -> Policy:
    """Build a Policy from a parsed mapping.

    The mapping is a `policy.toml` document or the inline `[policy]` table from
    aim.toml. Recognized sections: [repos], [artifacts], [profiles], [risk]
    (+ [risk.rules]), and [[rule]] custom rules.

    Args:
        data: The parsed policy mapping.

    Returns:
        The constructed Policy.

    Raises:
        PolicyError: If a custom rule entry is invalid.
    """
    repos_t = data.get("repos", {}) or {}
    artifacts_t = data.get("artifacts", {}) or {}
    profiles_t = data.get("profiles", {}) or {}
    archetypes_t = data.get("archetypes", {}) or {}
    risk_t = dict(data.get("risk", {}) or {})
    rules_t = dict(risk_t.pop("rules", {}) or {})

    preset_overrides = {k: v for k, v in rules_t.items() if k != "custom"}
    risk = RiskSettings(
        mode=str(risk_t.get("mode", "block")),
        classifier=bool(risk_t.get("classifier", False)),
        llm_judge=bool(risk_t.get("llm_judge", False)),
        model_id=str(risk_t.get("model_id", DEFAULT_MODEL_ID)),
        block_threshold=str(risk_t.get("block_threshold", "high")),
        escalate_threshold=str(risk_t.get("escalate_threshold", "medium")),
        judge=risk_t.get("judge"),
        load_custom=bool(rules_t.get("custom", True)),
        allow_override=bool(risk_t.get("allow_override", True)),
        preset_overrides=preset_overrides,
    )
    custom_rules: list[RiskRule] = []
    for raw in data.get("rule", []) or []:
        try:
            custom_rules.append(RiskRule(**raw))
        except Exception as exc:
            raise PolicyError(f"invalid custom rule: {exc}") from exc
    return Policy(
        version=int(data.get("version", 1)),
        name=str(data.get("name", "local")),
        blocked_repos=list(repos_t.get("blocked", [])),
        blocked_skills=list(artifacts_t.get("blocked_skills", [])),
        blocked_agents=list(artifacts_t.get("blocked_agents", [])),
        blocked_rules=list(artifacts_t.get("blocked_rules", [])),
        blocked_mcp=list(artifacts_t.get("blocked_mcp", [])),
        allowed_profiles=list(profiles_t.get("allowed", [])),
        allowed_archetypes=list(archetypes_t.get("allowed", [])),
        risk=risk,
        custom_rules=custom_rules,
    )


def from_toml(text: str) -> Policy:
    """Parse a `policy.toml` document into a Policy.

    Args:
        text: The raw policy.toml content.

    Returns:
        The parsed Policy.

    Raises:
        PolicyError: If the document is not valid TOML.
    """
    try:
        return from_mapping(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"invalid policy.toml: {exc}") from exc


def to_mapping(policy: Policy) -> dict:
    """Build the policy mapping shared by org `policy.toml` and aim.toml's `[policy]`.

    Custom `[[rule]]` entries are included inline.

    Args:
        policy: The policy to serialize.

    Returns:
        The mapping representation.
    """
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
    if policy.allowed_archetypes:
        doc["archetypes"] = {"allowed": policy.allowed_archetypes}
    risk: dict = {
        "mode": policy.risk.mode,
        "classifier": policy.risk.classifier,
        "llm_judge": policy.risk.llm_judge,
        "model_id": policy.risk.model_id,
        "block_threshold": policy.risk.block_threshold,
        "escalate_threshold": policy.risk.escalate_threshold,
        "allow_override": policy.risk.allow_override,
    }
    if policy.risk.judge is not None:
        risk["judge"] = policy.risk.judge
    rules: dict = dict(policy.risk.preset_overrides)
    rules["custom"] = policy.risk.load_custom
    risk["rules"] = rules
    doc["risk"] = risk
    if policy.custom_rules:
        doc["rule"] = [r.model_dump() for r in policy.custom_rules]
    return doc


def to_toml(policy: Policy) -> str:
    """Serialize a Policy to a self-contained `policy.toml` document.

    Risk settings and custom `[[rule]]` entries are emitted together, matching
    aim.toml's `[policy]` layout.

    Args:
        policy: The policy to serialize.

    Returns:
        The policy.toml text.
    """
    return tomli_w.dumps(to_mapping(policy))


def parse_rules_toml(text: str) -> list[RiskRule]:
    """Parse custom `[[rule]]` entries from a standalone rules toml document.

    Args:
        text: The raw rules toml content.

    Returns:
        The parsed custom rules.

    Raises:
        PolicyError: If the document or any rule entry is invalid.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"invalid rules toml: {exc}") from exc
    out: list[RiskRule] = []
    for raw in data.get("rule", []):
        try:
            out.append(RiskRule(**raw))
        except Exception as exc:
            raise PolicyError(f"invalid rule: {exc}") from exc
    return out


def render_rules_toml(rules: list[RiskRule]) -> str:
    """Serialize custom `[[rule]]` entries to a standalone rules toml document.

    Args:
        rules: The custom rules to serialize.

    Returns:
        The rules toml text.
    """
    return tomli_w.dumps({"rule": [r.model_dump() for r in rules]})


def compute_hash(policy: Policy) -> str:
    """Compute a deterministic content hash over the fully-resolved policy.

    Covers fields, risk settings, and custom rules. Used to pin the policy in the
    lockfile and detect drift/tampering. Custom rules are canonicalized by `id` so
    re-importing the same rules in a different order does not change the hash.

    Args:
        policy: The policy to hash.

    Returns:
        The hex-encoded SHA-256 digest.
    """
    data = policy.model_dump(mode="json")
    data["custom_rules"] = sorted(data.get("custom_rules", []), key=lambda r: r["id"])
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedPolicy:
    """A resolved policy together with its source, origin repo, and content hash."""

    policy: Policy
    source: str  # "builtin" | "local" | "org"
    repo: str | None  # org policy repo url, else None
    hash: str | None  # policy content hash, else None


def _policy_clone_dir(repo_url: str) -> Path:
    """Return the local cache directory for a bare clone of the org policy repo."""
    key = hashlib.sha256(normalize_repo_url(repo_url).encode("utf-8")).hexdigest()[:16]
    return paths.user_cache_dir() / "policy" / key


def _org_snapshot_key(repo_url: str) -> str:
    """Return the GlobalSetting key under which the org snapshot for a repo is cached."""
    digest = hashlib.sha256(normalize_repo_url(repo_url).encode("utf-8")).hexdigest()[:16]
    return f"policy:org_snapshot:{digest}"


def fetch_org_policy(
    repo_url: str, ref: str = "HEAD", *, allow_insecure: bool = False
) -> tuple[Policy, str]:
    """Fetch the policy repo and read its `policy.toml` at the resolved commit.

    Bare-clones or fetches the repo and reads the self-contained policy.toml (risk
    settings + inline `[[rule]]` together). This is a network op: call only from
    bind/refresh/validate, never from the lock/deploy hot path.

    Args:
        repo_url: The org policy repo URL.
        ref: The git ref to resolve (defaults to HEAD).
        allow_insecure: Permit a plaintext-http repo URL.

    Returns:
        A tuple of the parsed Policy and the resolved commit sha.
    """
    # The policy repo is the trust root — never fetch it over plaintext http.
    content_guard.require_secure_url(repo_url, allow_insecure=allow_insecure)
    paths.ensure_global_dirs()
    dest = _policy_clone_dir(repo_url)
    backend = git.get_backend()
    if dest.exists():
        backend.fetch(dest)
    else:
        backend.clone_bare(repo_url, dest)
    sha = backend.resolve_ref(dest, ref)
    pol = from_toml(backend.cat_file(dest, sha, "policy.toml"))
    return pol, sha


def cache_org_snapshot(repo_url: str, ref: str, sha: str, pol: Policy) -> None:
    """Pin a fetched org policy in the DB (a cache) so resolution stays offline.

    Args:
        repo_url: The org policy repo URL.
        ref: The git ref the snapshot was fetched at.
        sha: The resolved commit sha.
        pol: The fetched policy to cache.
    """
    blob = json.dumps(
        {
            "repo": repo_url,
            "ref": ref,
            "sha": sha,
            "fetched_at": datetime.now(UTC).isoformat(),
            "hash": compute_hash(pol),
            "policy": pol.model_dump(mode="json"),
        }
    )
    with db.session() as session:
        key = _org_snapshot_key(repo_url)
        row = session.get(GlobalSetting, key)
        if row is None:
            session.add(GlobalSetting(key=key, value=blob))
        else:
            row.value = blob
        session.commit()


def load_org_snapshot(repo_url: str) -> ResolvedPolicy | None:
    """Return the cached org policy for `repo_url`, read from the DB (no network).

    A corrupt snapshot is treated as 'no usable policy' so resolution can fail closed.

    Args:
        repo_url: The org policy repo URL.

    Returns:
        The cached ResolvedPolicy, or None if absent or corrupt.
    """
    with db.session() as session:
        row = session.get(GlobalSetting, _org_snapshot_key(repo_url))
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        pol = Policy.model_validate(data["policy"])
        return ResolvedPolicy(pol, "org", data["repo"], data["hash"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def org_snapshot_sha(repo_url: str) -> str | None:
    """Return the cached snapshot's commit sha for `repo_url`, or None if unavailable."""
    with db.session() as session:
        row = session.get(GlobalSetting, _org_snapshot_key(repo_url))
    if row is None:
        return None
    try:
        return json.loads(row.value).get("sha")
    except json.JSONDecodeError:
        return None


def _snapshot_fetched_at(repo_url: str) -> datetime | None:
    """Return when the cached snapshot for `repo_url` was fetched, or None if unknown."""
    with db.session() as session:
        row = session.get(GlobalSetting, _org_snapshot_key(repo_url))
    if row is None:
        return None
    try:
        ts = json.loads(row.value).get("fetched_at")
        return datetime.fromisoformat(ts) if ts else None
    except (json.JSONDecodeError, ValueError):
        return None


# Repos whose TTL-refresh was already attempted this process — avoids re-fetching
# per artifact when sync fans the deploy gates out across threads.
_refresh_attempted: set[str] = set()
_refresh_lock = threading.Lock()


def reset_refresh_state() -> None:
    """Clear the once-per-process refresh guard (used by tests)."""
    with _refresh_lock:
        _refresh_attempted.clear()


def _snapshot_expired(repo_url: str) -> bool:
    """Return whether the cached snapshot for `repo_url` is missing or older than the TTL."""
    fetched = _snapshot_fetched_at(repo_url)
    return fetched is None or (datetime.now(UTC) - fetched) > _ORG_TTL


def _maybe_refresh_org(repo_url: str, ref: str) -> None:
    """Opportunistically re-fetch a stale org snapshot, at most once per process per repo.

    Best-effort: offline/unreachable keeps whatever is cached, and a fresh
    (within-TTL) snapshot is left untouched so day-to-day resolution stays offline.

    Args:
        repo_url: The org policy repo URL.
        ref: The git ref to fetch.
    """
    with _refresh_lock:
        if repo_url in _refresh_attempted or not _snapshot_expired(repo_url):
            return
        _refresh_attempted.add(repo_url)  # claim before the slow fetch
    try:
        pol, sha = fetch_org_policy(repo_url, ref)
        cache_org_snapshot(repo_url, ref, sha, pol)
    except (git.GitError, content_guard.InsecureTransportError, OSError):
        pass  # fall back to the existing cache (if any)


def bind(repo_url: str, ref: str = "HEAD", *, allow_insecure: bool = False) -> ResolvedPolicy:
    """Fetch an org policy repo and pin its snapshot.

    The caller (CLI) writes `[policy] scope = "org"` into the project's aim.toml;
    this only warms the cache.

    Args:
        repo_url: The org policy repo URL.
        ref: The git ref to resolve (defaults to HEAD).
        allow_insecure: Permit a plaintext-http repo URL.

    Returns:
        The resolved org policy.
    """
    pol, sha = fetch_org_policy(repo_url, ref, allow_insecure=allow_insecure)
    cache_org_snapshot(repo_url, ref, sha, pol)
    return ResolvedPolicy(pol, "org", repo_url, compute_hash(pol))


_INLINE_KEYS = ("repos", "artifacts", "profiles", "archetypes", "risk", "rule", "name", "version")


def _read_policy_section(project_root: Path) -> dict:
    """Return the project's `[policy]` table from aim.toml ({} if absent)."""
    from aim.core import declarations

    try:
        decl = declarations.load(project_root)
    except declarations.DeclarationsNotFoundError:
        return {}
    return decl.policy or {}


# Public read/write of the project's [policy] section (used by the CLI).
project_policy_section = _read_policy_section


def set_project_policy(project_root: Path, section: dict) -> None:
    """Write the `[policy]` table into the project's aim.toml.

    Args:
        project_root: The project root containing aim.toml.
        section: The `[policy]` table contents to persist.
    """
    from aim.core import declarations

    decl = declarations.load_or_default(project_root)
    decl.policy = section
    declarations.save(project_root, decl)


def _resolve_org(section: dict) -> ResolvedPolicy:
    """Resolve an org-scoped policy section from its cached snapshot.

    Args:
        section: The project's `[policy]` table.

    Returns:
        The cached org ResolvedPolicy.

    Raises:
        PolicyError: If no repo is configured, or no usable snapshot is cached
            (fails closed rather than falling back to permissive).
    """
    repo = section.get("repo")
    if not repo:
        raise PolicyError("[policy] scope = 'org' requires a 'repo' URL")
    _maybe_refresh_org(repo, section.get("ref", "HEAD"))
    snapshot = load_org_snapshot(repo)
    if snapshot is None:
        # Fail CLOSED rather than silently fall back to permissive.
        raise PolicyError(
            f"project policy points at org repo {repo!r} but no usable snapshot is "
            "cached and it could not be fetched. Run `aim policy refresh`."
        )
    return snapshot


def refresh_org_policy(project_root: Path) -> ResolvedPolicy | None:
    """Re-fetch the project's org policy (if scope='org') and update the cache.

    Args:
        project_root: The project root containing aim.toml.

    Returns:
        The refreshed ResolvedPolicy, or None if the project is not org-scoped.
    """
    section = _read_policy_section(project_root)
    repo = section.get("repo")
    if section.get("scope") != "org" or not repo:
        return None
    return bind(repo, section.get("ref", "HEAD"))


def resolve_effective(project_root: Path | None = None) -> ResolvedPolicy:
    """Resolve the effective policy from the project's aim.toml `[policy]` table.

    scope='org' uses the cached org snapshot, opportunistically re-fetching it once
    per process if it's older than the TTL (best-effort; offline keeps the cache,
    fails closed only if there is no cache). Inline policy is built directly; an
    absent or empty section yields the permissive built-in.

    Args:
        project_root: The project root, or None for the built-in policy.

    Returns:
        The resolved policy with its source, repo, and hash.
    """
    if project_root is None:
        return ResolvedPolicy(builtin_policy(), "builtin", None, None)
    section = _read_policy_section(project_root)
    if section.get("scope") == "org" or section.get("repo"):
        return _resolve_org(section)
    if section.get("scope") == "local" or any(k in section for k in _INLINE_KEYS):
        pol = from_mapping(section)
        return ResolvedPolicy(pol, "local", None, compute_hash(pol))
    return ResolvedPolicy(builtin_policy(), "builtin", None, None)


def effective_policy(project_root: Path | None = None) -> Policy:
    """Resolve the effective policy without computing a hash.

    The cheap path used by the per-artifact deploy gates. Fails closed if a
    project's org policy has no snapshot.

    Args:
        project_root: The project root, or None for the built-in policy.

    Returns:
        The resolved Policy.
    """
    if project_root is None:
        return builtin_policy()
    section = _read_policy_section(project_root)
    if section.get("scope") == "org" or section.get("repo"):
        return _resolve_org(section).policy
    if section.get("scope") == "local" or any(k in section for k in _INLINE_KEYS):
        return from_mapping(section)
    return builtin_policy()


def assert_repo_allowed(policy: Policy, alias: str, url: str) -> None:
    """Raise if a repo is blocked by policy, matching on alias or normalized URL.

    Args:
        policy: The active policy.
        alias: The repo's local alias.
        url: The repo's git URL.

    Raises:
        PolicyViolationError: If the repo is blocked.
    """
    if not policy.blocked_repos:
        return
    norm = normalize_repo_url(url)
    for entry in policy.blocked_repos:
        if entry == alias or normalize_repo_url(entry) == norm:
            raise PolicyViolationError(
                f"repo {alias!r} ({url}) is blocked by policy {policy.name!r}"
            )


def assert_artifact_allowed(policy: Policy, kind: str, qualified_name: str) -> None:
    """Raise if a skill/agent/rule is blocked by policy.

    Args:
        policy: The active policy.
        kind: The artifact kind ("skill", "agent", or "rule").
        qualified_name: The artifact's qualified name.

    Raises:
        PolicyViolationError: If the artifact is blocked.
    """
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
    """Raise if an MCP server is blocked by policy, matching on alias or registry name.

    Args:
        policy: The active policy.
        alias: The MCP server's local alias.
        registry_name: The MCP server's registry name.

    Raises:
        PolicyViolationError: If the MCP server is blocked.
    """
    if not policy.blocked_mcp:
        return
    if alias in policy.blocked_mcp or registry_name in policy.blocked_mcp:
        raise PolicyViolationError(
            f"MCP server {alias!r} ({registry_name}) is blocked by policy {policy.name!r}"
        )


def assert_profile_allowed(policy: Policy, layout_profile_name: str | None) -> None:
    """Raise if a layout profile is not in the policy's allow-list.

    An empty allow-list or a None profile name permits everything.

    Args:
        policy: The active policy.
        layout_profile_name: The layout profile to check, or None.

    Raises:
        PolicyViolationError: If the profile is not allowed.
    """
    if not policy.allowed_profiles or layout_profile_name is None:
        return
    if layout_profile_name not in policy.allowed_profiles:
        raise PolicyViolationError(
            f"layout profile {layout_profile_name!r} is not in the policy "
            f"allow-list {policy.allowed_profiles} (policy {policy.name!r})"
        )


def assert_archetype_allowed(policy: Policy, qualified_name: str | None) -> None:
    """Raise if an instruction archetype is not in the policy's allow-list.

    An empty allow-list or a None selection permits everything (None means the
    built-in instruction template, which is always allowed).

    Args:
        policy: The active policy.
        qualified_name: The archetype's qualified name, or None for the built-in.

    Raises:
        PolicyViolationError: If the archetype is not allowed.
    """
    if not policy.allowed_archetypes or qualified_name is None:
        return
    if qualified_name not in policy.allowed_archetypes:
        raise PolicyViolationError(
            f"instruction archetype {qualified_name!r} is not in the policy "
            f"allow-list {policy.allowed_archetypes} (policy {policy.name!r})"
        )
