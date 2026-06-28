"""User-editable project declarations stored in `aim.toml`.

`aim init` creates this file; `aim lock` resolves it into `aim.lock.toml`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from aim.core import paths
from aim.core.models import CURRENT_DECLARATIONS_VERSION, ProjectDeclarations


class DeclarationsNotFoundError(FileNotFoundError):
    """Raised when no `aim.toml` exists at the expected project path."""


class DeclarationsVersionError(RuntimeError):
    """Raised when an `aim.toml` cannot be migrated to the current version."""


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Forward-migrate raw declarations to CURRENT_DECLARATIONS_VERSION.

    Args:
        raw: Parsed `aim.toml` mapping, possibly from an older manifest version.

    Returns:
        The same mapping mutated in place to the current manifest version.

    Raises:
        DeclarationsVersionError: If the version is not an int, is newer than
            supported, or lists pre-v3 rules by name (no automatic migration).
    """
    version = raw.get("manifest_version", 1)
    if not isinstance(version, int):
        raise DeclarationsVersionError(
            f"manifest_version must be int, got {type(version).__name__}"
        )
    if version > CURRENT_DECLARATIONS_VERSION:
        raise DeclarationsVersionError(
            f"aim.toml version {version} is newer than supported ({CURRENT_DECLARATIONS_VERSION}). "
            "Upgrade aim."
        )
    if version < 2:
        # v2 drops agent_dialect and adds rules_mode default on the active layout profile.
        raw.pop("agent_dialect", None)
        raw["manifest_version"] = 2
        version = 2
    if version < 3:
        # v3 makes rules repo-sourced, SHA-pinned artifacts. The pre-v3 format
        # listed rules by name (`rule = ["..."]`) against a local library that no
        # longer exists. Rule-less projects upgrade cleanly; projects that listed
        # rules by name must re-add them (there is no automatic migration).
        if raw.get("rules"):
            raise DeclarationsVersionError(
                "aim.toml lists rules by name (pre-v3). v3 makes rules repo-sourced. "
                "Re-add each rule via `aim rule add <git-url> <name>`."
            )
        raw["rules"] = []
        raw["manifest_version"] = 3
        version = 3
    if version < 4:
        # v4 adds the optional [policy] governance table. Additive.
        raw.setdefault("policy", {})
        raw["manifest_version"] = 4
        version = 4
    if version < 5:
        # v5 adds the optional [instruction_archetype] selection. Additive — absence
        # means the built-in instruction template is used.
        raw["manifest_version"] = 5
        version = 5
    if version < 6:
        # v6 adds the optional [template] provenance table. Additive — absence means
        # the project was not stamped from a shared template.
        raw["manifest_version"] = 6
        version = 6
    if version < 7:
        # v7 drops the vestigial instruction_template field. The AGENTS.md base is
        # the [instruction_archetype] selection, or the built-in default when absent.
        raw.pop("instruction_template", None)
        raw["manifest_version"] = 7
        version = 7
    if version < 8:
        # v8 renames [instruction_archetype] -> [archetype] and makes it always
        # present: absent selection becomes the built-in `default`.
        old = raw.pop("instruction_archetype", None)
        raw["archetype"] = old if old is not None else {"qualified_name": "default"}
        raw["manifest_version"] = 8
        version = 8
    if version < 9:
        # v9 adds the optional [[plugin]] surface. Additive.
        raw.setdefault("plugins", [])
        raw["manifest_version"] = 9
        version = 9
    if version < 10:
        # v10 makes the on-disk repo identity source-agnostic: `[repos]` is rekeyed
        # from alias->url to repo_id->normalized_url, and every artifact's
        # qualified_name/repo_alias is rewritten to id form. Deterministic, so two
        # teammates' independent first-run rewrites converge byte-for-byte.
        _rekey_v9_to_v10_id_form(raw)
        raw["manifest_version"] = 10
    return raw


def _rekey_v9_to_v10_id_form(raw: dict[str, Any]) -> None:
    """Rewrite a v9 alias-keyed declarations mapping to v10 id-keyed form, in place.

    Args:
        raw: The parsed v9 declarations mapping (plural artifact keys).
    """
    from aim.core import policy

    repos_map = raw.get("repos", {}) or {}
    alias_to_id = {alias: policy.repo_id_for_url(url) for alias, url in repos_map.items()}
    raw["repos"] = {
        alias_to_id[alias]: policy.normalize_repo_url(url)
        for alias, url in repos_map.items()
        if alias in alias_to_id
    }
    for key in ("skills", "agents", "rules", "plugins"):
        for entry in raw.get(key, []) or []:
            _rekey_artifact_to_id(entry, alias_to_id)
    archetype = raw.get("archetype")
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _rekey_artifact_to_id(archetype, alias_to_id)


def _rekey_artifact_to_id(entry: dict[str, Any], alias_to_id: dict[str, str]) -> None:
    """Rewrite one artifact dict's repo_alias/qualified_name from alias to id form.

    Args:
        entry: The artifact mapping to mutate in place.
        alias_to_id: Mapping of local alias to repo identity token.
    """
    alias = entry.get("repo_alias")
    repo_id = alias_to_id.get(alias) if isinstance(alias, str) else None
    if repo_id is None:
        return
    entry["repo_alias"] = repo_id
    qn = entry.get("qualified_name")
    if isinstance(qn, str) and "/" in qn:
        entry["qualified_name"] = f"{repo_id}/{qn.split('/', 1)[1]}"


# TOML uses singular array-of-table headers; the models use plural field names.
_TOML_READ_MAP = {
    "skill": "skills",
    "subagent": "agents",
    "mcp_server": "mcp_servers",
    "rule": "rules",
    "plugin": "plugins",
}
_TOML_WRITE_MAP = {v: k for k, v in _TOML_READ_MAP.items()}

# Match TOML table headers like [[skills]], [skills.current], [[skills.history]], etc.
_TABLE_HEADER_RE = re.compile(r"^(\[\[?)(\w+)((?:\.\w+)*)?(\]\]?)$")


def _singularize_table_headers(text: str) -> str:
    """Rewrite plural TOML table headers back to their singular on-disk form.

    Args:
        text: Serialized TOML using the models' plural field names.

    Returns:
        The TOML text with array-of-table headers singularized per
        `_TOML_WRITE_MAP`.
    """
    out: list[str] = []
    for line in text.splitlines():
        match = _TABLE_HEADER_RE.match(line.strip())
        if match:
            prefix, base, suffix, suffix_bracket = match.groups()
            singular = _TOML_WRITE_MAP.get(base)
            if singular is not None:
                line = f"{prefix}{singular}{suffix or ''}{suffix_bracket}"
        out.append(line)
    return "\n".join(out)


def _resolve_disk_repos(disk_repos: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve the on-disk id-keyed `[repos]` table to local alias/url mappings.

    For each `repo_id -> normalized_url` on disk, the local alias is the registered
    repo's alias when the identity is known on this machine, else a default
    `owner-repo` alias derived from the URL — WITHOUT cloning or registering. The
    in-memory URL is the local repo's chosen clone URL (ssh/https) when registered,
    else the normalized URL from disk, so per-machine variation never reaches disk.

    Args:
        disk_repos: The on-disk `[repos]` mapping (repo_id -> normalized URL).

    Returns:
        A pair ``(id_to_alias, alias_to_url)`` for rebuilding alias-form models.
    """
    from aim.core import repos as repos_mod

    id_to_alias: dict[str, str] = {}
    alias_to_url: dict[str, str] = {}
    for repo_id, disk_url in disk_repos.items():
        repo = repos_mod.get_by_id(repo_id)
        if repo is not None:
            id_to_alias[repo_id] = repo.alias
            alias_to_url[repo.alias] = repo.url
        else:
            alias = repos_mod.derive_default_alias(disk_url)
            id_to_alias[repo_id] = alias
            alias_to_url[alias] = disk_url
    return id_to_alias, alias_to_url


def _from_disk(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate an on-disk id-keyed declarations mapping to alias-form, in place.

    Resolves each `repo_id` to a local alias (registered or default-derived, no
    clone), rewrites `[repos]` to `alias -> local_url`, and rewrites every
    artifact's `qualified_name`/`repo_alias` from id form to alias form. Downstream
    code then operates entirely on aliases, unchanged.

    Args:
        raw: The migrated (id-form) declarations mapping.

    Returns:
        The same mapping, mutated to alias form.
    """
    disk_repos = raw.get("repos", {}) or {}
    id_to_alias, alias_to_url = _resolve_disk_repos(disk_repos)
    raw["repos"] = dict(sorted(alias_to_url.items()))
    for key in ("skills", "agents", "rules", "plugins"):
        for entry in raw.get(key, []) or []:
            _rekey_artifact_from_id(entry, id_to_alias)
    archetype = raw.get("archetype")
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _rekey_artifact_from_id(archetype, id_to_alias)
    pol = raw.get("policy")
    if isinstance(pol, dict) and isinstance(pol.get("repo"), str) and pol["repo"]:
        from aim.core import policy

        pol["repo"] = policy.local_policy_repo_url(pol["repo"])
    _template_from_id(raw.get("template"))
    return raw


def _rekey_artifact_from_id(entry: dict[str, Any], id_to_alias: dict[str, str]) -> None:
    """Rewrite one artifact dict's repo_alias/qualified_name from id to alias form.

    Args:
        entry: The artifact mapping to mutate in place.
        id_to_alias: Mapping of repo identity token to local alias.
    """
    repo_id = entry.get("repo_alias")
    alias = id_to_alias.get(repo_id) if isinstance(repo_id, str) else None
    if alias is None:
        return
    entry["repo_alias"] = alias
    qn = entry.get("qualified_name")
    if isinstance(qn, str) and "/" in qn:
        entry["qualified_name"] = f"{alias}/{qn.split('/', 1)[1]}"


def _template_to_id(tmpl: dict[str, Any] | None) -> None:
    """Rewrite a declared template's identity (`qualified_name`/`repo_alias`/`url`) to id form.

    The template repo is registry-backed, so it gets the same id-form treatment as
    artifacts: a content-addressed `repo_alias`/`qualified_name` and a normalized URL,
    so the committed file is byte-identical across machines.

    Args:
        tmpl: The serialized template mapping (or None when no template is recorded).
    """
    from aim.core import policy

    if not (isinstance(tmpl, dict) and isinstance(tmpl.get("url"), str) and tmpl["url"]):
        return
    repo_id = policy.repo_id_for_url(tmpl["url"])
    tmpl["url"] = policy.normalize_repo_url(tmpl["url"])
    if isinstance(tmpl.get("repo_alias"), str):
        tmpl["repo_alias"] = repo_id
    qn = tmpl.get("qualified_name")
    if isinstance(qn, str) and "/" in qn:
        tmpl["qualified_name"] = f"{repo_id}/{qn.split('/', 1)[1]}"


def _template_from_id(tmpl: dict[str, Any] | None) -> None:
    """Resolve a declared template's id-form identity to the local alias/URL (no clone).

    The local alias and clone URL come from the registered repo when its identity is
    known on this machine, else a default `owner-repo` alias and the on-disk URL — so
    downstream `profile check/update` resolves the template via the local index.

    Args:
        tmpl: The serialized (id-form) template mapping (or None).
    """
    from aim.core import policy
    from aim.core import repos as repos_mod

    if not (isinstance(tmpl, dict) and isinstance(tmpl.get("url"), str) and tmpl["url"]):
        return
    repo = repos_mod.get_by_id(policy.repo_id_for_url(tmpl["url"]))
    if repo is not None:
        alias, local_url = repo.alias, repo.url
    else:
        alias, local_url = repos_mod.derive_default_alias(tmpl["url"]), tmpl["url"]
    tmpl["repo_alias"] = alias
    tmpl["url"] = local_url
    qn = tmpl.get("qualified_name")
    if isinstance(qn, str) and "/" in qn:
        tmpl["qualified_name"] = f"{alias}/{qn.split('/', 1)[1]}"


def _to_disk(decl: ProjectDeclarations) -> dict[str, Any]:
    """Serialize declarations to an on-disk id-keyed mapping (source-agnostic).

    Builds `[repos]` as `repo_id -> normalize_repo_url(url)` and rewrites every
    artifact's `qualified_name`/`repo_alias` to id form, so the committed file is
    byte-identical across machines for the same project. The plugin
    `marketplace_name` is already id-based (``aim-<repo_id>``) and passes through.

    Args:
        decl: The in-memory (alias-form) declarations.

    Returns:
        A JSON-mode mapping ready for TOML serialization.
    """
    from aim.core import policy

    data = decl.model_dump(mode="json", exclude_none=True)
    alias_repos = data.get("repos", {}) or {}
    alias_to_id = {alias: policy.repo_id_for_url(url) for alias, url in alias_repos.items()}
    # Sort by repo_id so the on-disk [repos] order is identical across machines
    # regardless of local alias insertion order.
    data["repos"] = {
        rid: norm
        for rid, norm in sorted(
            (alias_to_id[alias], policy.normalize_repo_url(url))
            for alias, url in alias_repos.items()
        )
    }
    for key in ("skills", "agents", "rules", "plugins"):
        for entry in data.get(key, []) or []:
            _rekey_artifact_to_id(entry, alias_to_id)
    archetype = data.get("archetype")
    if isinstance(archetype, dict) and archetype.get("repo_alias") is not None:
        _rekey_artifact_to_id(archetype, alias_to_id)
    # The org-policy repo is a direct clone target with no [repos] indirection. Store it
    # NORMALIZED for cross-machine determinism; the local clone form is re-derived on load
    # from the per-machine record `policy.record_policy_repo_url` writes at bind time.
    pol = data.get("policy")
    if isinstance(pol, dict) and isinstance(pol.get("repo"), str) and pol["repo"]:
        pol["repo"] = policy.normalize_repo_url(pol["repo"])
    _template_to_id(data.get("template"))
    return data


def load(project_root: Path) -> ProjectDeclarations:
    """Load and migrate the project's `aim.toml` into a validated model.

    Args:
        project_root: Directory whose `aim.toml` should be read.

    Returns:
        The parsed, migrated, and validated project declarations.

    Raises:
        DeclarationsNotFoundError: If no `aim.toml` exists at `project_root`.
    """
    path = paths.project_declarations_path(project_root)
    if not path.exists():
        raise DeclarationsNotFoundError(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for singular, plural in _TOML_READ_MAP.items():
        if singular in raw:
            raw[plural] = raw.pop(singular)
    migrated = _migrate(raw)
    alias_form = _from_disk(migrated)
    return ProjectDeclarations.model_validate(alias_form)


def load_or_default(project_root: Path) -> ProjectDeclarations:
    """Load the project's declarations, or return empty defaults if absent.

    Args:
        project_root: Directory whose `aim.toml` should be read.

    Returns:
        The loaded declarations, or a fresh empty `ProjectDeclarations`.
    """
    try:
        return load(project_root)
    except DeclarationsNotFoundError:
        return ProjectDeclarations()


def save(project_root: Path, declarations: ProjectDeclarations) -> None:
    """Serialize declarations to the project's `aim.toml`.

    Args:
        project_root: Directory whose `aim.toml` should be written.
        declarations: The declarations to persist.
    """
    path = paths.project_declarations_path(project_root)
    data = _to_disk(declarations)
    text = tomli_w.dumps(data)
    text = _singularize_table_headers(text)
    path.write_text(text + "\n", encoding="utf-8")


def on_disk_version(project_root: Path) -> int:
    """Return the `manifest_version` recorded in the on-disk `aim.toml`, before migration.

    Args:
        project_root: Directory whose `aim.toml` should be read.

    Returns:
        The raw `manifest_version` stored in the file (1 if the field is absent).

    Raises:
        DeclarationsNotFoundError: If no `aim.toml` exists at `project_root`.
    """
    path = paths.project_declarations_path(project_root)
    if not path.exists():
        raise DeclarationsNotFoundError(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return raw.get("manifest_version", 1)


def bump(project_root: Path) -> tuple[int, int]:
    """Migrate the on-disk `aim.toml` to `CURRENT_DECLARATIONS_VERSION` and re-save it.

    Re-saving rewrites the file even when already current, so newly-defaulted tables
    (such as the always-present `[archetype]` block) are materialized.

    Args:
        project_root: Directory whose `aim.toml` should be bumped.

    Returns:
        A `(from_version, to_version)` pair.

    Raises:
        DeclarationsNotFoundError: If no `aim.toml` exists at `project_root`.
        DeclarationsVersionError: If the file's version is invalid or newer than supported.
    """
    from_version = on_disk_version(project_root)
    save(project_root, load(project_root))
    return from_version, CURRENT_DECLARATIONS_VERSION


def _update_skill(project_root: Path, installed: object) -> None:
    """Mirror an installed skill into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledSkill` describing the skill to record.
    """
    from aim.core.models import DeclaredSkill, InstalledSkill

    assert isinstance(installed, InstalledSkill)
    decl = load_or_default(project_root)
    declared = DeclaredSkill(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        target_dir=installed.target_dir,
        pin=installed.pin,
        track=installed.track,
        risk_acknowledged=installed.risk_acknowledged,
    )
    decl.skills = [s for s in decl.skills if s.qualified_name != installed.qualified_name]
    decl.skills.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def _prune_repo_if_unused(decl: ProjectDeclarations, alias: str) -> None:
    """Drop the `[repos]` binding for `alias` once nothing references it.

    Install paths add these bindings, so an orphaned one only lingers after the
    last artifact (skill, agent, or rule) from that repo is removed.

    Args:
        decl: The declarations to prune in place.
        alias: The repo alias to remove if unreferenced.
    """
    used = (
        any(s.repo_alias == alias for s in decl.skills)
        or any(a.repo_alias == alias for a in decl.agents)
        or any(r.repo_alias == alias for r in decl.rules)
        or any(p.repo_alias == alias for p in decl.plugins)
        or decl.archetype.repo_alias == alias
    )
    if not used:
        decl.repos.pop(alias, None)


def _remove_skill(project_root: Path, qualified_name: str) -> None:
    """Drop a declared skill and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the skill to remove.
    """
    decl = load_or_default(project_root)
    decl.skills = [s for s in decl.skills if s.qualified_name != qualified_name]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def _update_agent(project_root: Path, installed: object) -> None:
    """Mirror an installed agent into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledAgent` describing the agent to record.
    """
    from aim.core.models import DeclaredAgent, InstalledAgent

    assert isinstance(installed, InstalledAgent)
    decl = load_or_default(project_root)
    declared = DeclaredAgent(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        target_path=installed.target_path,
        pin=installed.pin,
        track=installed.track,
        risk_acknowledged=installed.risk_acknowledged,
    )
    decl.agents = [a for a in decl.agents if a.qualified_name != installed.qualified_name]
    decl.agents.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def _remove_agent(project_root: Path, qualified_name: str) -> None:
    """Drop a declared agent and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the agent to remove.
    """
    decl = load_or_default(project_root)
    decl.agents = [a for a in decl.agents if a.qualified_name != qualified_name]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def _update_rule(project_root: Path, installed: object) -> None:
    """Mirror an installed rule into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledRule` describing the rule to record.
    """
    from aim.core.models import DeclaredRule, InstalledRule

    assert isinstance(installed, InstalledRule)
    decl = load_or_default(project_root)
    declared = DeclaredRule(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        pin=installed.pin,
        track=installed.track,
        risk_acknowledged=installed.risk_acknowledged,
    )
    decl.rules = [r for r in decl.rules if r.qualified_name != installed.qualified_name]
    decl.rules.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def set_archetype(project_root: Path, installed: object) -> None:
    """Record the selected archetype (singleton) as the AGENTS.md base in `aim.toml`.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledArchetype` describing the selected archetype.
    """
    from aim.core.models import DeclaredArchetype, InstalledArchetype

    assert isinstance(installed, InstalledArchetype)
    decl = load_or_default(project_root)
    decl.archetype = DeclaredArchetype(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        source_path=installed.source_path,
        pin=installed.pin,
        track=installed.track,
        risk_acknowledged=installed.risk_acknowledged,
    )
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def clear_archetype(project_root: Path) -> None:
    """Revert the AGENTS.md base to the built-in default, pruning the repo binding."""
    from aim.core.models import DeclaredArchetype

    decl = load_or_default(project_root)
    previous_alias = decl.archetype.repo_alias
    decl.archetype = DeclaredArchetype()  # built-in default
    if previous_alias is not None:
        _prune_repo_if_unused(decl, previous_alias)
    save(project_root, decl)


def _remove_rule(project_root: Path, qualified_name: str) -> None:
    """Drop a declared rule and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the rule to remove.
    """
    decl = load_or_default(project_root)
    decl.rules = [r for r in decl.rules if r.qualified_name != qualified_name]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def _update_mcp(project_root: Path, installed: object) -> None:
    """Mirror an installed MCP server into the declarations file.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledMcpServer` describing the server to record.
    """
    from aim.core.models import DeclaredMcpServer, InstalledMcpServer

    assert isinstance(installed, InstalledMcpServer)
    decl = load_or_default(project_root)
    declared = DeclaredMcpServer(
        alias=installed.alias,
        registry_name=installed.registry_name,
        preferred_transport=installed.entry.type if installed.entry else None,
        overrides=installed.overrides or {},
    )
    decl.mcp_servers = [m for m in decl.mcp_servers if m.alias != installed.alias]
    decl.mcp_servers.append(declared)
    save(project_root, decl)


def _remove_mcp(project_root: Path, alias: str) -> None:
    """Drop a declared MCP server by its alias.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        alias: The alias of the MCP server to remove.
    """
    decl = load_or_default(project_root)
    decl.mcp_servers = [m for m in decl.mcp_servers if m.alias != alias]
    save(project_root, decl)


def _update_plugin(
    project_root: Path, installed: object, *, marketplace_name: str | None = None
) -> None:
    """Mirror an installed plugin into the declarations file.

    The declaration keeps the *upstream* (semantic) marketplace name so the
    Claude-facing `.claude/settings.json` key reads naturally (e.g. ``memex``),
    while the lock keeps the id-based ``aim-<repo_id>`` form. Resolution order for
    the declared name: an explicit ``marketplace_name`` (the upstream name from the
    index at install time), else a previously declared value, else the installed
    (id-based) value. Consequence: on update/rollback (no explicit name) the
    first-seen declared name wins — a later upstream marketplace rename is not
    picked up automatically. This is intentional: the committed key stays stable.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        installed: An `InstalledPlugin` describing the plugin to record.
        marketplace_name: Upstream marketplace name to record, when known.
    """
    from aim.core.models import DeclaredPlugin, InstalledPlugin

    assert isinstance(installed, InstalledPlugin)
    decl = load_or_default(project_root)
    prior = next(
        (
            p
            for p in decl.plugins
            if p.qualified_name == installed.qualified_name and p.flavor == installed.flavor
        ),
        None,
    )
    declared = DeclaredPlugin(
        qualified_name=installed.qualified_name,
        repo_alias=installed.repo_alias,
        flavor=installed.flavor,
        source_path=installed.source_path,
        marketplace_name=(
            marketplace_name
            or (prior.marketplace_name if prior else None)
            or installed.marketplace_name
        ),
        pin=installed.pin,
        track=installed.track,
        risk_acknowledged=installed.risk_acknowledged,
    )
    # Same name under a different flavor coexists, so replace only the entry that
    # matches BOTH qualified_name AND flavor — not every entry sharing the name.
    decl.plugins = [
        p
        for p in decl.plugins
        if not (p.qualified_name == installed.qualified_name and p.flavor == installed.flavor)
    ]
    decl.plugins.append(declared)
    decl.repos[installed.repo_alias] = installed.repo_url
    save(project_root, decl)


def _remove_plugin(project_root: Path, qualified_name: str, flavor: str) -> None:
    """Drop a declared plugin and prune its repo binding if now unused.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        qualified_name: The `repo_alias/name` of the plugin to remove.
        flavor: The flavor of the plugin to remove; same name under a different
            flavor is left in place.
    """
    decl = load_or_default(project_root)
    decl.plugins = [
        p for p in decl.plugins if not (p.qualified_name == qualified_name and p.flavor == flavor)
    ]
    _prune_repo_if_unused(decl, qualified_name.split("/", 1)[0])
    save(project_root, decl)


def set_template_provenance(project_root: Path, declared: object) -> None:
    """Record (or replace) the project's template provenance in `aim.toml`.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        declared: A `DeclaredTemplate` describing the applied template.
    """
    from aim.core.models import DeclaredTemplate

    assert isinstance(declared, DeclaredTemplate)
    decl = load_or_default(project_root)
    decl.template = declared
    save(project_root, decl)


def set_template_members(project_root: Path, members: list[str]) -> None:
    """Update the member-artifact set of the recorded template provenance.

    No-op if the project has no template provenance.

    Args:
        project_root: Directory whose `aim.toml` should be updated.
        members: Qualified names (plus ``mcp:<alias>``) the template installed.
    """
    decl = load_or_default(project_root)
    if decl.template is None:
        return
    decl.template.members = members
    save(project_root, decl)
