"""Project templates (profiles) — named bundles of init settings.

A template snapshots a project's `(symlinks, rules, skills, agents, mcp_servers,
layout_profile)` so you can stamp out new projects from it.
Stored as JSON under `user_config_dir/profiles/<name>.json`.

Skills, agents and rules are frozen to the exact commit `sha` they resolved to in
the source project's lock, so applying a template reproduces identical versions.
The template's content hash is therefore a complete fingerprint of the resolved
bundle. Templates can be imported/exported as TOML and shared via a git repo.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aim.core import agent_install as agent_install_mod
from aim.core import init as init_mod
from aim.core import install as install_mod
from aim.core import lock as lock_mod
from aim.core import mcp_install as mcp_install_mod
from aim.core import mcp_registry as mcp_registry_mod
from aim.core import paths
from aim.core import rule_install as rule_install_mod
from aim.core import sync as sync_mod
from aim.core.validation import is_valid_mirror_name, is_valid_rule_name

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProfileNameError(ValueError):
    """Raised when a profile name fails the naming rules."""

    pass


class ProfileNotFoundError(KeyError):
    """Raised when a named profile does not exist on disk."""

    pass


class ProfileTomlError(ValueError):
    """Invalid TOML or content for a project profile."""

    pass


class TemplateNotLockedError(RuntimeError):
    """Cannot save a template from a project that has no lock to read SHAs from."""

    pass


class ProfileSkill(BaseModel):
    """A skill reference in a template, frozen to an exact commit `sha`.

    `sha` is the resolved commit captured from the project's lock at save time, so
    applying the template reproduces the same version. None means install latest.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    sha: str | None = None


class ProfileAgent(BaseModel):
    """A sub-agent reference in a template, frozen to an exact commit `sha`."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    sha: str | None = None


class ProfileRule(BaseModel):
    """A rule reference in a template, frozen to an exact commit `sha`."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    sha: str | None = None

    @field_validator("qualified_name")
    @classmethod
    def _validate_qualified_name(cls, value: str) -> str:
        """Validate the rule qualified name as ``<alias>/<rule>``.

        Raises:
            ValueError: If the name is not a valid qualified rule name.
        """
        name = value.partition("/")[2]
        if not name or not is_valid_rule_name(name):
            raise ValueError(f"rule qualified name {value!r} invalid (expected <alias>/<rule>)")
        return value


class ProfileMcpServer(BaseModel):
    """An MCP server reference in a profile, by registry name and local alias."""

    model_config = ConfigDict(extra="forbid")

    registry_name: str
    alias: str
    transport: str | None = None
    overrides: dict[str, object] | None = None


class ProfileRepo(BaseModel):
    """A source repo a profile's artifacts come from, identified by stable url.

    The local `alias` is a nickname; the `url` is the portable identity used to
    map the repo to a (possibly differently-aliased) local registration.
    """

    model_config = ConfigDict(extra="forbid")

    alias: str
    url: str


class Profile(BaseModel):
    """A named bundle of init settings and artifact references for stamping projects."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    layout_profile: str | None = None
    symlinks: list[str] = Field(default_factory=list)
    repos: list[ProfileRepo] = Field(default_factory=list)
    rules: list[ProfileRule] = Field(default_factory=list)
    skills: list[ProfileSkill] = Field(default_factory=list)
    agents: list[ProfileAgent] = Field(default_factory=list)
    mcp_servers: list[ProfileMcpServer] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name_field(cls, value: str) -> str:
        """Validate the profile name against the allowed character set.

        Args:
            value: The proposed profile name.

        Returns:
            The validated name unchanged.

        Raises:
            ProfileNameError: If the name is not lowercase alphanumeric/_/-.
        """
        if not _NAME_RE.fullmatch(value):
            raise ProfileNameError(
                f"profile name {value!r} invalid: must be lowercase alphanumeric, _, or -"
            )
        return value

    @field_validator("symlinks")
    @classmethod
    def _validate_symlink_names(cls, values: list[str]) -> list[str]:
        """Validate each symlink entry as a legal mirror filename.

        Returns:
            The validated list unchanged.

        Raises:
            ValueError: If any entry is not a valid mirror name.
        """
        for value in values:
            if not is_valid_mirror_name(value):
                raise ValueError(f"filename {value!r} invalid")
        return values


def _profile_path(name: str) -> Path:
    """Return the on-disk JSON path for a profile of the given name."""
    return paths.user_config_dir() / "profiles" / f"{name}.json"


def _validate_name(name: str) -> None:
    """Raise if the profile name is not lowercase alphanumeric/_/-.

    Raises:
        ProfileNameError: If the name violates the naming rules.
    """
    if not _NAME_RE.fullmatch(name):
        raise ProfileNameError(
            f"profile name {name!r} invalid: must be lowercase alphanumeric, _, or -"
        )


def save(profile: Profile) -> Path:
    """Write a profile to disk as JSON and return its path."""
    _validate_name(profile.name)
    paths.ensure_global_dirs()
    path = _profile_path(profile.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2) + "\n")
    return path


def load(name: str) -> Profile:
    """Load a profile by name from disk.

    Raises:
        ProfileNotFoundError: If no profile file exists for the name.
    """
    _validate_name(name)
    path = _profile_path(name)
    if not path.exists():
        raise ProfileNotFoundError(name)
    return Profile.model_validate(json.loads(path.read_text()))


def list_profiles() -> list[Profile]:
    """Load and return all valid profiles, skipping corrupt files."""
    dir_ = paths.user_config_dir() / "profiles"
    if not dir_.exists():
        return []
    out: list[Profile] = []
    for path in sorted(dir_.glob("*.json")):
        if not _NAME_RE.fullmatch(path.stem):
            continue
        try:
            out.append(Profile.model_validate(json.loads(path.read_text())))
        except Exception:  # pragma: no cover — corrupt file
            continue
    return out


def delete(name: str) -> bool:
    """Delete a profile by name.

    Returns:
        True if the profile existed and was removed, False otherwise.
    """
    _validate_name(name)
    path = _profile_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


_TOML_KEY_MAP = {
    "repo": "repos",
    "rule": "rules",
    "skill": "skills",
    "subagent": "agents",
    "mcp_server": "mcp_servers",
}


def parse_toml(text: str, *, source: str | None = None) -> Profile:
    """Parse a project profile from a TOML string.

    TOML uses singular array-of-table headers per item (``[[skill]]``,
    ``[[subagent]]``, ``[[mcp_server]]``); these are mapped to the plural
    field names on the Profile model.

    Args:
        text: The raw TOML content.
        source: Optional label for the source, used in error messages.

    Returns:
        The parsed and validated Profile.

    Raises:
        ProfileTomlError: If the TOML is invalid or fails profile validation.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileTomlError(f"invalid TOML in {source or 'profile'}: {exc}") from exc
    for singular, plural in _TOML_KEY_MAP.items():
        if singular in raw:
            raw[plural] = raw.pop(singular)
    raw.pop("instruction_template", None)  # back-compat: dropped field, ignore if present
    try:
        return Profile.model_validate(raw)
    except (ProfileNameError, ValueError) as exc:
        raise ProfileTomlError(str(exc)) from exc


def render_toml(profile: Profile) -> str:
    """Serialize a project profile to TOML."""
    lines: list[str] = []
    lines.append(f'name = "{_escape_toml_string(profile.name)}"')
    if profile.description:
        lines.append(f'description = "{_escape_toml_string(profile.description)}"')
    if profile.layout_profile:
        lines.append(f'layout_profile = "{_escape_toml_string(profile.layout_profile)}"')
    lines.append(f"symlinks = {_render_string_list(profile.symlinks)}")
    lines.append("")
    for repo in profile.repos:
        lines.append("[[repo]]")
        lines.append(f'alias = "{_escape_toml_string(repo.alias)}"')
        lines.append(f'url = "{_escape_toml_string(repo.url)}"')
        lines.append("")
    for skill in profile.skills:
        lines.append("[[skill]]")
        lines.append(f'qualified_name = "{_escape_toml_string(skill.qualified_name)}"')
        if skill.sha:
            lines.append(f'sha = "{_escape_toml_string(skill.sha)}"')
        lines.append("")
    for agent in profile.agents:
        lines.append("[[subagent]]")
        lines.append(f'qualified_name = "{_escape_toml_string(agent.qualified_name)}"')
        if agent.sha:
            lines.append(f'sha = "{_escape_toml_string(agent.sha)}"')
        lines.append("")
    for rule in profile.rules:
        lines.append("[[rule]]")
        lines.append(f'qualified_name = "{_escape_toml_string(rule.qualified_name)}"')
        if rule.sha:
            lines.append(f'sha = "{_escape_toml_string(rule.sha)}"')
        lines.append("")
    for mcp in profile.mcp_servers:
        lines.append("[[mcp_server]]")
        lines.append(f'registry_name = "{_escape_toml_string(mcp.registry_name)}"')
        lines.append(f'alias = "{_escape_toml_string(mcp.alias)}"')
        if mcp.transport:
            lines.append(f'transport = "{_escape_toml_string(mcp.transport)}"')
        non_null_overrides = (
            {k: v for k, v in mcp.overrides.items() if v is not None} if mcp.overrides else {}
        )
        if non_null_overrides:
            lines.append("  [mcp_server.overrides]")
            for key, value in non_null_overrides.items():
                lines.append(f"  {key} = {_render_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _escape_toml_string(value: str) -> str:
    """Escape a string for safe inclusion in a TOML basic (double-quoted) string."""

    def _replace(match: re.Match[str]) -> str:
        """Return the TOML escape sequence for a single matched control char."""
        char = match.group(0)
        if char == "\\":
            return "\\\\"
        if char == '"':
            return '\\"'
        if char == "\n":
            return "\\n"
        if char == "\t":
            return "\\t"
        code = ord(char)
        return f"\\u{code:04x}"

    return re.sub(r'[\\"\x00-\x1f\x7f]', _replace, value)


def _render_string_list(values: list[str]) -> str:
    """Render a list of strings as a TOML inline array of escaped strings."""
    if not values:
        return "[]"
    parts = [f'"{_escape_toml_string(v)}"' for v in values]
    return "[" + ", ".join(parts) + "]"


def _render_toml_value(value: object) -> str:
    """Render a scalar value as its TOML literal, falling back to a string."""
    if isinstance(value, str):
        return f'"{_escape_toml_string(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if value is None:
        return "false"
    # Fall back to string for anything else.
    return f'"{_escape_toml_string(str(value))}"'


def from_project(name: str, project_root: Path) -> Profile:
    """Snapshot a project into a template, freezing each artifact to its locked SHA.

    Reads the resolved commit SHAs from `aim.lock.toml` so the template reproduces
    exactly what is installed. A project that declares artifacts but has no lock
    cannot be templated (the SHAs would be missing).

    Raises:
        TemplateNotLockedError: The project declares artifacts but has no
            `aim.lock.toml` to read their SHAs from.
    """
    from aim.core import declarations as declarations_mod
    from aim.core import manifest as manifest_mod

    _validate_name(name)

    decl = declarations_mod.load_or_default(project_root)
    try:
        m = manifest_mod.load(project_root)
    except manifest_mod.ManifestNotFoundError as exc:
        if decl.skills or decl.agents or decl.rules:
            raise TemplateNotLockedError(
                "cannot save a template: this project declares artifacts but has no "
                "aim.lock.toml. Run `aim lock` first so the template can freeze each "
                "artifact to its exact SHA."
            ) from exc
        # An empty project (no artifacts) templates cleanly with no SHAs to freeze.
        return Profile(
            name=name,
            layout_profile=decl.layout_profile,
            symlinks=list(decl.symlinks),
        )

    # Source repos by url, taken from the lock, so the template resolves on another
    # machine even when repos are registered under different aliases.
    repo_urls: dict[str, str] = {}
    for s in m.skills:
        repo_urls[s.repo_alias] = s.repo_url
    for a in m.agents:
        repo_urls[a.repo_alias] = a.repo_url
    for r in m.rules:
        repo_urls[r.repo_alias] = r.repo_url
    repos = [ProfileRepo(alias=alias, url=url) for alias, url in sorted(repo_urls.items())]

    return Profile(
        name=name,
        layout_profile=decl.layout_profile,
        symlinks=list(decl.symlinks),
        repos=repos,
        rules=[ProfileRule(qualified_name=r.qualified_name, sha=r.current.sha) for r in m.rules],
        skills=[ProfileSkill(qualified_name=s.qualified_name, sha=s.current.sha) for s in m.skills],
        agents=[ProfileAgent(qualified_name=a.qualified_name, sha=a.current.sha) for a in m.agents],
        mcp_servers=[
            ProfileMcpServer(
                registry_name=ms.registry_name,
                alias=ms.alias,
                transport=ms.entry.type if ms.entry else None,
                overrides=ms.overrides,
            )
            for ms in m.mcp_servers
        ],
    )


@dataclass
class ProfileApplyResult:
    """Outcome of applying a profile: installed and skipped artifacts per kind."""

    project_root: Path
    init_result: object
    installed_skills: list[str] = field(default_factory=list)
    skipped_skills: list[str] = field(default_factory=list)
    installed_agents: list[str] = field(default_factory=list)
    skipped_agents: list[str] = field(default_factory=list)
    installed_mcp: list[str] = field(default_factory=list)
    skipped_mcp: list[str] = field(default_factory=list)
    installed_rules: list[str] = field(default_factory=list)
    skipped_rules: list[str] = field(default_factory=list)


def resolve_for_apply(
    profile: Profile,
    project_root: Path,
    *,
    allow_insecure: bool = False,
) -> Profile:
    """Map a template's source repos to local registrations, cloning any missing.

    Artifacts are referenced by ``<alias>/<name>``, but the template's alias is
    only a local nickname. Repos are matched by their stable url: a repo already
    registered under a different alias has its qualified names rewritten to that
    local alias; a repo not registered at all is cloned from the url recorded in
    the template's ``[[repo]]`` block (after a policy block-list check), since the
    template already carries everything needed to fetch it.

    Args:
        profile: The template to resolve (unmodified; a rewritten copy is returned).
        project_root: Project whose effective policy gates any auto-registration.
        allow_insecure: Permit insecure (non-https) urls when registering.

    Returns:
        The template with artifact aliases rewritten to local aliases.

    Raises:
        policy.PolicyViolationError: A referenced repo url is on the policy
            block-list.
        git.GitError: A referenced repo url could not be cloned.
    """
    from aim.core import policy as policy_mod
    from aim.core import repos as repos_mod

    if not profile.repos:
        return profile

    local_by_url = {r.url: r.alias for r in repos_mod.list_repos()}
    alias_rewrite: dict[str, str] = {}
    pol = None
    for required in profile.repos:
        local_alias = local_by_url.get(required.url)
        if local_alias is None:
            # The template carries the url, so just register it (policy-screened).
            if pol is None:
                pol = policy_mod.effective_policy(project_root)
            policy_mod.assert_repo_allowed(pol, required.alias, required.url)
            repos_mod.add(required.alias, required.url, allow_insecure=allow_insecure)
        elif local_alias != required.alias:
            alias_rewrite[required.alias] = local_alias

    if not alias_rewrite:
        return profile

    def _rewrite(qn: str) -> str:
        """Swap a qualified name's alias prefix for its local equivalent."""
        alias, sep, rest = qn.partition("/")
        return f"{alias_rewrite[alias]}/{rest}" if sep and alias in alias_rewrite else qn

    return profile.model_copy(
        update={
            "skills": [
                s.model_copy(update={"qualified_name": _rewrite(s.qualified_name)})
                for s in profile.skills
            ],
            "agents": [
                a.model_copy(update={"qualified_name": _rewrite(a.qualified_name)})
                for a in profile.agents
            ],
            "rules": [
                r.model_copy(update={"qualified_name": _rewrite(r.qualified_name)})
                for r in profile.rules
            ],
        }
    )


def apply(
    name: str,
    project_root: Path,
    *,
    allow_insecure: bool = False,
) -> ProfileApplyResult:
    """Apply a project template by name.

    Source repos the template needs are auto-registered from the urls in its
    ``[[repo]]`` block (policy-screened). A bare ``name`` loads a locally-saved
    template and applies it leniently (an artifact still unresolved after repo
    registration is skipped). A qualified ``<alias>/<name>`` loads the template
    from a registered repo and applies it strictly — a still-unresolved artifact
    is an error, so a shared template never silently applies an empty bundle.

    Args:
        name: A saved template name, or a repo-qualified ``<alias>/<name>``.
        project_root: The project directory to apply the template to.
        allow_insecure: Permit insecure sources during lock and sync.

    Returns:
        A ProfileApplyResult recording installed and skipped artifacts.
    """
    if "/" in name:
        from aim.core import hashing, repo_templates
        from aim.core import repos as repos_mod
        from aim.core.models import DeclaredTemplate

        row = repo_templates.index_row(name)
        content = repo_templates.read_template_content(name)
        template_source = DeclaredTemplate(
            qualified_name=name,
            repo_alias=row.repo_alias,
            url=repos_mod.get(row.repo_alias).url,
            ref=row.indexed_at_sha,
            template_hash=hashing.hash_text(content),
        )
        return apply_profile(
            parse_toml(content, source=name),
            project_root,
            strict_resolution=True,
            allow_insecure=allow_insecure,
            template_source=template_source,
        )
    return apply_profile(load(name), project_root, allow_insecure=allow_insecure)


def apply_profile(
    profile: Profile,
    project_root: Path,
    *,
    strict_resolution: bool = False,
    allow_insecure: bool = False,
    template_source: object | None = None,
) -> ProfileApplyResult:
    """Apply a profile to a project, then install its artifacts and sync.

    Runs init from the profile, locks declarations, installs skills, agents,
    MCP servers and rules, and syncs agent instruction files. Source repos are
    auto-registered from the template's ``[[repo]]`` block.

    Args:
        profile: The profile to apply.
        project_root: The project directory to apply the profile to.
        strict_resolution: Raise (instead of skipping) when an artifact is not
            indexed locally even after its source repo was registered. Used for
            repo-sourced templates, where a silent skip would hide an empty bundle.
        allow_insecure: Permit insecure sources during lock and sync.
        template_source: A `DeclaredTemplate` to record as this project's template
            provenance (for repo-sourced templates). Its `members` are filled in
            from the artifacts actually installed.

    Returns:
        A ProfileApplyResult recording installed and skipped artifacts.
    """
    profile = resolve_for_apply(profile, project_root, allow_insecure=allow_insecure)
    init_result = init_mod.run(
        init_mod.InitOptions(
            project_root=project_root,
            layout_profile=profile.layout_profile,
            symlinks=tuple(profile.symlinks),
        )
    )

    # Record template provenance after init (which would otherwise not know about
    # it) and before lock, so the lock mirrors the template pin into the manifest.
    from aim.core import declarations as declarations_mod

    if template_source is not None:
        declarations_mod.set_template_provenance(project_root, template_source)

    # Resolve declarations into a lock so subsequent installs preserve symlinks.
    asyncio.run(
        lock_mod.run(lock_mod.LockOptions(project_root=project_root, allow_insecure=allow_insecure))
    )

    installed_skills: list[str] = []
    skipped_skills: list[str] = []
    for ps in profile.skills:
        try:
            install_mod.install(project_root, ps.qualified_name, pin=ps.sha)
            installed_skills.append(ps.qualified_name)
        except install_mod.SkillNotIndexedError:
            if strict_resolution:
                raise
            skipped_skills.append(ps.qualified_name)

    installed_agents: list[str] = []
    skipped_agents: list[str] = []
    for pa in profile.agents:
        try:
            agent_install_mod.install(project_root, pa.qualified_name, pin=pa.sha)
            installed_agents.append(pa.qualified_name)
        except agent_install_mod.AgentNotIndexedError:
            if strict_resolution:
                raise
            skipped_agents.append(pa.qualified_name)

    installed_mcp: list[str] = []
    skipped_mcp: list[str] = []
    for pm in profile.mcp_servers:
        try:
            mcp_install_mod.install(
                project_root,
                pm.registry_name,
                alias=pm.alias,
                preferred_transport=pm.transport,
                overrides=pm.overrides,
                force=False,
            )
            installed_mcp.append(pm.alias)
        except (
            mcp_registry_mod.McpRegistryError,
            mcp_registry_mod.McpMappingError,
            mcp_install_mod.McpAliasConflictError,
            mcp_install_mod.McpAliasInvalidError,
        ):
            skipped_mcp.append(pm.alias)

    installed_rules: list[str] = []
    skipped_rules: list[str] = []
    for pr in profile.rules:
        try:
            rule_install_mod.install(project_root, pr.qualified_name, pin=pr.sha)
            installed_rules.append(pr.qualified_name)
        except rule_install_mod.RuleNotIndexedError:
            if strict_resolution:
                raise
            skipped_rules.append(pr.qualified_name)

    # Record which artifacts the template owns, so a later `profile update` can
    # add new members and remove dropped ones without touching user additions.
    if template_source is not None:
        members = [
            *installed_skills,
            *installed_agents,
            *installed_rules,
            *(f"mcp:{alias}" for alias in installed_mcp),
        ]
        declarations_mod.set_template_members(project_root, members)

    # Reconcile agent instruction files so symlinks promised by the profile are
    # actually written to disk.
    asyncio.run(
        sync_mod.run(
            sync_mod.SyncOptions(
                project_root=project_root,
                sync_agents=True,
                layout_profile=profile.layout_profile,
                allow_insecure=allow_insecure,
            )
        )
    )

    return ProfileApplyResult(
        project_root=project_root,
        init_result=init_result,
        installed_skills=installed_skills,
        skipped_skills=skipped_skills,
        installed_agents=installed_agents,
        skipped_agents=skipped_agents,
        installed_mcp=installed_mcp,
        skipped_mcp=skipped_mcp,
        installed_rules=installed_rules,
        skipped_rules=skipped_rules,
    )


# ---------------------------------------------------------------------------
# Template drift detection & update (CI/CD primitives)
# ---------------------------------------------------------------------------


class NoProjectTemplateError(RuntimeError):
    """The project was not stamped from a shared template, so there is nothing
    to check or update."""


@dataclass(frozen=True)
class TemplateCheckResult:
    """Outcome of `template check`: whether the applied template drifted upstream.

    A template is a frozen, hash-identified bundle. Its `template_hash` (a content
    hash of the template toml, which embeds each artifact's exact SHA) fully
    fingerprints the resolved state, so a single hash comparison detects drift.
    """

    has_template: bool
    qualified_name: str | None = None
    locked_hash: str | None = None
    upstream_hash: str | None = None

    @property
    def drift(self) -> bool:
        """Whether the upstream template hash differs from the applied one."""
        return self.upstream_hash is not None and self.upstream_hash != self.locked_hash

    @property
    def up_to_date(self) -> bool:
        """Whether the applied template still matches its upstream version."""
        return not self.drift


def _member_keys(profile: Profile) -> list[str]:
    """Return the template-owned member keys for a resolved profile."""
    return [
        *(s.qualified_name for s in profile.skills),
        *(a.qualified_name for a in profile.agents),
        *(r.qualified_name for r in profile.rules),
        *(f"mcp:{m.alias}" for m in profile.mcp_servers),
    ]


def check(project_root: Path) -> TemplateCheckResult:
    """Detect whether the applied template has drifted from its upstream version.

    Compares the template hash recorded at apply time against a hash of the current
    upstream template toml. Because the template freezes every artifact to an exact
    SHA, this one comparison is a complete drift signal.

    Args:
        project_root: The project to check.

    Returns:
        A TemplateCheckResult; ``has_template`` is False when the project was not
        stamped from a shared template.
    """
    from aim.core import declarations as declarations_mod
    from aim.core import hashing, repo_templates
    from aim.core import manifest as manifest_mod

    decl = declarations_mod.load_or_default(project_root)
    if decl.template is None:
        return TemplateCheckResult(has_template=False)

    try:
        m = manifest_mod.load(project_root)
    except manifest_mod.ManifestNotFoundError:
        m = None

    locked_hash = (m.template_hash if m else None) or decl.template.template_hash
    try:
        upstream_hash: str | None = hashing.hash_text(
            repo_templates.read_template_content(decl.template.qualified_name)
        )
    except repo_templates.TemplateNotIndexedError:
        upstream_hash = None

    return TemplateCheckResult(
        has_template=True,
        qualified_name=decl.template.qualified_name,
        locked_hash=locked_hash,
        upstream_hash=upstream_hash,
    )


@dataclass(frozen=True)
class TemplateDiff:
    """Structural delta between the recorded template and its upstream version."""

    qualified_name: str
    added: list[str]
    removed: list[str]
    unchanged: list[str]


def diff(project_root: Path) -> TemplateDiff:
    """Preview which template-owned artifacts an update would add or remove.

    Args:
        project_root: The project whose recorded template is compared upstream.

    Returns:
        A TemplateDiff of added/removed/unchanged member keys.

    Raises:
        NoProjectTemplateError: The project was not stamped from a template.
    """
    from aim.core import declarations as declarations_mod
    from aim.core import repo_templates

    decl = declarations_mod.load_or_default(project_root)
    if decl.template is None:
        raise NoProjectTemplateError("this project was not stamped from a shared template")
    qn = decl.template.qualified_name
    new_profile = resolve_for_apply(repo_templates.load_template(qn), project_root)
    new_members = _member_keys(new_profile)
    old = set(decl.template.members)
    new = set(new_members)
    return TemplateDiff(
        qualified_name=qn,
        added=sorted(new - old),
        removed=sorted(old - new),
        unchanged=sorted(old & new),
    )


@dataclass
class TemplateUpdateResult:
    """Outcome of `profile update`: the re-apply result plus removed members."""

    apply_result: ProfileApplyResult
    removed: list[str]


def update_from_template(
    project_root: Path,
    *,
    allow_insecure: bool = False,
) -> TemplateUpdateResult:
    """Converge a project to the latest version of its recorded template.

    Removes template-owned artifacts dropped upstream, then re-applies the
    template (adding new members, updating existing ones, re-locking, re-syncing,
    and re-recording provenance). Artifacts the user added on top of the template
    are left untouched.

    Args:
        project_root: The project to update.
        allow_insecure: Permit insecure sources during lock and sync.

    Returns:
        A TemplateUpdateResult with the re-apply result and the removed members.

    Raises:
        NoProjectTemplateError: The project was not stamped from a template.
    """
    from aim.core import declarations as declarations_mod
    from aim.core import repo_templates

    decl = declarations_mod.load_or_default(project_root)
    if decl.template is None:
        raise NoProjectTemplateError("this project was not stamped from a shared template")
    qn = decl.template.qualified_name

    new_profile = resolve_for_apply(
        repo_templates.load_template(qn), project_root, allow_insecure=allow_insecure
    )
    new_members = set(_member_keys(new_profile))
    removed = [member for member in decl.template.members if member not in new_members]
    for member in removed:
        _delete_member(project_root, member)

    apply_result = apply(qn, project_root, allow_insecure=allow_insecure)
    return TemplateUpdateResult(apply_result=apply_result, removed=removed)


def _delete_member(project_root: Path, member: str) -> None:
    """Remove a single template-owned artifact by its member key."""
    from aim.core import manifest as manifest_mod

    if member.startswith("mcp:"):
        try:
            mcp_install_mod.delete(project_root, member[len("mcp:") :])
        except mcp_install_mod.McpServerNotInstalledError:
            pass
        return
    try:
        m = manifest_mod.load(project_root)
    except manifest_mod.ManifestNotFoundError:
        return
    if any(s.qualified_name == member for s in m.skills):
        install_mod.delete(project_root, member)
    elif any(a.qualified_name == member for a in m.agents):
        agent_install_mod.delete(project_root, member)
    elif any(r.qualified_name == member for r in m.rules):
        rule_install_mod.delete(project_root, member)


@dataclass(frozen=True)
class BumpChange:
    """A single artifact whose pinned SHA was advanced inside a template."""

    qualified_name: str
    old_sha: str | None
    new_sha: str


class TemplateArtifactNotFoundError(KeyError):
    """The named artifact is not part of the template (for a single-artifact bump)."""


def bump(name: str, *, only: str | None = None, allow_insecure: bool = False) -> list[BumpChange]:
    """Advance a saved template's pinned artifact SHAs to the latest from their repos.

    This edits the *template* (not any project): each skill/agent/rule reference is
    re-resolved to the newest commit on its source repo and its ``sha`` is rewritten.
    Source repos the template names are auto-registered from its ``[[repo]]`` block.
    MCP servers carry no SHA and are left untouched.

    Args:
        name: The saved template to update.
        only: A single ``<alias>/<name>`` to bump; when None, bump every artifact.
        allow_insecure: Permit insecure (non-https) urls when registering repos.

    Returns:
        One BumpChange per artifact whose SHA actually advanced.

    Raises:
        TemplateArtifactNotFoundError: ``only`` names an artifact not in the template.
    """
    from aim.core import db
    from aim.core import repos as repos_mod
    from aim.core.models import AgentIndex, RuleIndex, SkillIndex

    profile = load(name)

    # Registering a repo is a global cache op with no project policy in scope
    # (same as `aim repo add`), so auto-register straight from the embedded urls.
    local_by_url = {r.url: r.alias for r in repos_mod.list_repos()}
    for repo in profile.repos:
        if repo.url not in local_by_url:
            local_by_url[repo.url] = repos_mod.add(
                repo.alias, repo.url, allow_insecure=allow_insecure
            ).alias
    url_by_alias = {r.alias: r.url for r in profile.repos}

    def _local_alias(qualified_name: str) -> tuple[str, str] | None:
        """Map a template qualified name to (local_alias, bare_name), or None."""
        template_alias, _, bare = qualified_name.partition("/")
        url = url_by_alias.get(template_alias)
        local = local_by_url.get(url) if url else None
        return (local, bare) if local else None

    def _latest(local_alias: str, source_path: str, artifact_name: str) -> str:
        return install_mod.resolve_install_version(
            local_alias, source_path, artifact_name=artifact_name
        ).sha

    changes: list[BumpChange] = []
    found = False
    with db.session() as session:
        for s in profile.skills:
            if only and s.qualified_name != only:
                continue
            found = found or only is not None
            mapped = _local_alias(s.qualified_name)
            if mapped is None:
                continue
            row = session.get(SkillIndex, f"{mapped[0]}/{mapped[1]}")
            if row is None:
                continue
            new_sha = _latest(mapped[0], row.source_path, "SKILL.md")
            if new_sha != s.sha:
                changes.append(BumpChange(s.qualified_name, s.sha, new_sha))
                s.sha = new_sha
        for a in profile.agents:
            if only and a.qualified_name != only:
                continue
            found = found or only is not None
            mapped = _local_alias(a.qualified_name)
            if mapped is None:
                continue
            row_a = session.get(AgentIndex, f"{mapped[0]}/{mapped[1]}")
            if row_a is None:
                continue
            new_sha = _latest(mapped[0], row_a.source_path, "AGENT.md")
            if new_sha != a.sha:
                changes.append(BumpChange(a.qualified_name, a.sha, new_sha))
                a.sha = new_sha
        for r in profile.rules:
            if only and r.qualified_name != only:
                continue
            found = found or only is not None
            mapped = _local_alias(r.qualified_name)
            if mapped is None:
                continue
            row_r = session.get(RuleIndex, f"{mapped[0]}/{mapped[1]}")
            if row_r is None:
                continue
            new_sha = _latest(mapped[0], row_r.rule_md_path, Path(row_r.rule_md_path).name)
            if new_sha != r.sha:
                changes.append(BumpChange(r.qualified_name, r.sha, new_sha))
                r.sha = new_sha

    if only and not found:
        raise TemplateArtifactNotFoundError(only)
    if changes:
        save(profile)
    return changes
