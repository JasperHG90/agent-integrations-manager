"""Single set of pydantic models. SQLModel tables are thin persistence shells over them.

Per the plan: one model layer to avoid drift between DB and JSON shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel


class RegisteredRepo(SQLModel, table=True):  # type: ignore[call-arg]
    """A skill source repo registered globally on this machine."""

    alias: str = SQLField(primary_key=True)
    url: str
    default_ref: str = "HEAD"  # branch/tag to track on refresh
    last_fetched_at: datetime | None = None
    last_sha: str | None = None


class SkillIndex(SQLModel, table=True):  # type: ignore[call-arg]
    """Discovered skill within a registered repo, used for `skill list/search`."""

    qualified_name: str = SQLField(primary_key=True)  # "<alias>/<skill_name>"
    repo_alias: str = SQLField(index=True)
    skill_name: str = SQLField(index=True)
    source_path: str  # path relative to repo root, e.g. "skills/code-review"
    skill_md_path: str | None = None  # path of the SKILL.md file relative to repo root
    title: str | None = None
    description: str | None = None
    indexed_at_sha: str
    # Comma-separated lists; SQLite has no array type. Use empty string for none.
    prereqs: str = ""  # qualified_names this skill requires (informational)
    provides: str = ""  # capability tags this skill claims to fulfill


class Template(SQLModel, table=True):  # type: ignore[call-arg]
    """A registered AGENTS.md template (built-in default plus user-registered)."""

    name: str = SQLField(primary_key=True)
    source: str  # "builtin", or a path / url for user-registered
    description: str | None = None


class LayoutProfile(SQLModel, table=True):  # type: ignore[call-arg]
    """Cache a global layout profile for sharing across projects.

    Repo-side global profiles are authoritative; the DB is only a cache.
    """

    name: str = SQLField(primary_key=True)
    content_hash: str
    toml_text: str
    updated_at: datetime


class GlobalSetting(SQLModel, table=True):  # type: ignore[call-arg]
    """Single-row key/value settings for machine-wide defaults."""

    key: str = SQLField(primary_key=True)
    value: str


class AgentIndex(SQLModel, table=True):  # type: ignore[call-arg]
    """Discovered sub-agent within a registered repo, used for `agent list/search`."""

    qualified_name: str = SQLField(primary_key=True)  # "<alias>/<agent_name>"
    repo_alias: str = SQLField(index=True)
    agent_name: str = SQLField(index=True)
    source_path: str  # path of the agent DIRECTORY relative to repo root
    agent_md_path: str | None = None  # path of the AGENT.md file relative to repo root
    title: str | None = None
    description: str | None = None
    indexed_at_sha: str
    tools: str = ""  # CSV for search only
    model: str | None = None


class RuleIndex(SQLModel, table=True):  # type: ignore[call-arg]
    """Discovered rule within a registered repo, used for rule search/overlay."""

    qualified_name: str = SQLField(primary_key=True)  # "<alias>/<rule_name>"
    repo_alias: str = SQLField(index=True)
    rule_name: str = SQLField(index=True)
    rule_md_path: str  # path of the .md file relative to repo root
    title: str | None = None
    description: str | None = None
    indexed_at_sha: str


class McpServerCache(SQLModel, table=True):  # type: ignore[call-arg]
    """Cached default MCP registry server definitions for instant TUI startup."""

    name: str = SQLField(primary_key=True)  # canonical registry server name
    definition_json: str  # raw registry server JSON (by_alias dump)
    fetched_at: datetime


class ArchetypeIndex(SQLModel, table=True):  # type: ignore[call-arg]
    """A discovered project-instruction archetype within a registered repo.

    An archetype is a directory (never the repo root) holding one or more standard
    instruction files (AGENTS.md / CLAUDE.md / GEMINI.md / OPENCODE.md). It is a
    selectable base for a project's AGENTS.md, used by `archetype list/search/use`.
    """

    qualified_name: str = SQLField(primary_key=True)  # "<alias>/<archetype_name>"
    repo_alias: str = SQLField(index=True)
    archetype_name: str = SQLField(index=True)
    source_path: str  # path of the archetype DIRECTORY relative to repo root
    instruction_path: str  # path of the chosen base instruction file (e.g. .../AGENTS.md)
    available: str = ""  # CSV of standard filenames present, e.g. "AGENTS.md,CLAUDE.md"
    title: str | None = None
    description: str | None = None
    indexed_at_sha: str


CURRENT_DECLARATIONS_VERSION = 5  # v5 adds the [instruction_archetype] selection


class DeclaredRepo(BaseModel):
    """Declare a skill source repo in `aim.toml`."""

    model_config = ConfigDict(extra="forbid")

    alias: str
    url: str
    default_ref: str | None = None


class DeclaredSkill(BaseModel):
    """Declare a skill to install in `aim.toml`."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    repo_alias: str
    source_path: str
    target_dir: str
    track: str | None = None
    pin: str | None = None


class DeclaredAgent(BaseModel):
    """Declare a sub-agent to install in `aim.toml`."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    repo_alias: str
    source_path: str
    target_path: str
    track: str | None = None
    pin: str | None = None


class DeclaredRule(BaseModel):
    """Declare a rule to install in `aim.toml`."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str
    repo_alias: str
    source_path: str  # path of the rule .md file relative to repo root
    track: str | None = None
    pin: str | None = None


class DeclaredMcpServer(BaseModel):
    """Declare an MCP server to install in `aim.toml`."""

    model_config = ConfigDict(extra="forbid")

    alias: str
    registry_name: str
    preferred_transport: str | None = None
    overrides: dict[str, object] = Field(default_factory=dict)


class DeclaredArchetype(BaseModel):
    """Declare the selected project-instruction archetype in `aim.toml`.

    Singleton: a project uses at most one archetype as its AGENTS.md base. None
    (the field absent) means the built-in instruction template is used instead.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str  # "<repo_alias>/<archetype_name>"
    repo_alias: str
    source_path: str  # the chosen base instruction file, relative to repo root
    track: str | None = None
    pin: str | None = None


class ProjectDeclarations(BaseModel):
    """User-editable project state stored in `aim.toml`."""

    model_config = ConfigDict(extra="forbid")

    manifest_version: int = CURRENT_DECLARATIONS_VERSION
    instruction_template: str = "default"
    # Selected instruction archetype (repo-sourced AGENTS.md base). None = built-in.
    instruction_archetype: DeclaredArchetype | None = None
    layout_profile: str | None = None
    symlinks: list[str] = Field(default_factory=list)
    rules: list[DeclaredRule] = Field(default_factory=list)
    # Governance policy for this project: {scope: "local"|"org", ...}. Empty = no
    # policy (permissive built-in). Stored as a raw mapping so the [policy] table is
    # parsed/interpreted by aim.core.policy (avoids a models<->policy import cycle).
    # Declared above `repos` so the [policy] table serializes near the top of aim.toml.
    policy: dict[str, Any] = Field(default_factory=dict)
    repos: dict[str, str] = Field(default_factory=dict)
    skills: list[DeclaredSkill] = Field(default_factory=list)
    agents: list[DeclaredAgent] = Field(default_factory=list)
    mcp_servers: list[DeclaredMcpServer] = Field(default_factory=list)


CURRENT_MANIFEST_VERSION = 11  # v11: pin the selected instruction archetype
HISTORY_CAP = 10


class SkillVersion(BaseModel):
    """Record a single installed version of a skill (tag, SHA, timestamp)."""

    model_config = ConfigDict(extra="forbid")

    tag: str | None = None
    sha: str
    installed_at: datetime

    def identifier(self) -> str:
        """Return the user-facing composite identifier `<tag>+<short_sha>` or SHA-only."""
        short = self.sha[:7]
        return f"{self.tag}+{short}" if self.tag else short


class InstalledSkill(BaseModel):
    """Record an installed skill and its version history in the manifest."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str  # "<repo_alias>/<skill_name>" at install time
    repo_alias: str  # the local alias at install time — survives upstream URL/name changes
    repo_url: str
    source_path: str  # path inside the source repo at install time
    target_dir: str  # path inside the project, e.g. ".claude/skills/code-review"
    current: SkillVersion
    history: list[SkillVersion] = Field(default_factory=list)
    content_hash: str | None = None  # sha256 of installed file tree (for drift detection)
    # v2 fields:
    pin: str | None = None  # exact tag — update refuses to advance past this
    track: str | None = None  # "latest-tag" | "<branch>" | "<ref>" — overrides repo.default_ref

    def push_history(self, new_current: SkillVersion) -> None:
        """Promote new_current to current, pushing the old one onto capped history."""
        self.history.insert(0, self.current)
        self.current = new_current
        if len(self.history) > HISTORY_CAP:
            del self.history[HISTORY_CAP:]


class McpClaudeEntry(BaseModel):
    """A single server entry inside Claude Code's `.mcp.json` -> `mcpServers`."""

    model_config = ConfigDict(extra="allow")

    type: str  # "stdio", "http", "sse", "ws"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class McpServerVersion(BaseModel):
    """Record a single installed version of an MCP server definition."""

    model_config = ConfigDict(extra="forbid")

    definition_hash: str  # sha256 of canonical registry definition JSON
    registry_version: str | None = None
    installed_at: datetime
    entry: McpClaudeEntry | None = None  # historical .mcp.json entry for rollback
    overrides: dict[str, object] | None = None  # overrides active for this version

    def identifier(self) -> str:
        """Return the registry version, falling back to a short definition hash."""
        return self.registry_version or self.definition_hash[:7]


class InstalledMcpServer(BaseModel):
    """Record an installed MCP server and its version history in the manifest."""

    model_config = ConfigDict(extra="forbid")

    alias: str  # local project alias (user-editable)
    registry_name: str  # canonical registry server name
    entry: McpClaudeEntry  # exact .mcp.json entry written
    entry_hash: str  # sha256 of canonical entry JSON
    current: McpServerVersion
    history: list[McpServerVersion] = Field(default_factory=list)
    overrides: dict[str, object] | None = None  # overrides to re-apply on update/lock

    def push_history(self, new_current: McpServerVersion) -> None:
        """Promote new_current to current, pushing the old one onto capped history."""
        self.history.insert(0, self.current)
        self.current = new_current
        if len(self.history) > HISTORY_CAP:
            del self.history[HISTORY_CAP:]


class InstalledAgent(BaseModel):
    """An installed sub-agent, mirroring InstalledSkill where applicable."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str  # "<repo_alias>/<agent_name>" at install time
    repo_alias: str  # the local alias at install time
    repo_url: str
    source_path: str  # path inside the source repo at install time
    target_path: str  # path inside the project, e.g. ".claude/agents/onboarding.md"
    current: SkillVersion
    history: list[SkillVersion] = Field(default_factory=list)
    content_hash: str | None = None  # sha256 of installed file text (drift detection)
    pin: str | None = None
    track: str | None = None

    def push_history(self, new_current: SkillVersion) -> None:
        """Promote new_current to current, pushing the old one onto capped history."""
        self.history.insert(0, self.current)
        self.current = new_current
        if len(self.history) > HISTORY_CAP:
            del self.history[HISTORY_CAP:]


class InstalledRule(BaseModel):
    """An installed rule, mirroring InstalledAgent. A rule is a single .md file;
    its render target is derived from the active layout profile (files dir or
    inline AGENTS.md region), so there is no per-rule target_path."""

    model_config = ConfigDict(extra="forbid")

    qualified_name: str  # "<repo_alias>/<rule_name>" at install time
    repo_alias: str  # the local alias at install time
    repo_url: str
    source_path: str  # path of the rule .md file inside the source repo
    current: SkillVersion
    history: list[SkillVersion] = Field(default_factory=list)
    content_hash: str | None = None  # sha256 of installed file text (drift detection)
    pin: str | None = None
    track: str | None = None

    def push_history(self, new_current: SkillVersion) -> None:
        """Promote new_current to current, pushing the old one onto capped history."""
        self.history.insert(0, self.current)
        self.current = new_current
        if len(self.history) > HISTORY_CAP:
            del self.history[HISTORY_CAP:]


class InstalledArchetype(BaseModel):
    """The locked project-instruction archetype recorded in `aim.lock.toml`.

    Singleton: pins the chosen base instruction file to a commit and content hash so
    `sync` can reproduce the project's AGENTS.md base deterministically.
    """

    model_config = ConfigDict(extra="forbid")

    qualified_name: str  # "<repo_alias>/<archetype_name>" at select time
    repo_alias: str
    repo_url: str
    source_path: str  # the base instruction file inside the source repo
    current: SkillVersion
    history: list[SkillVersion] = Field(default_factory=list)
    content_hash: str | None = None  # sha256 of the installed base body (drift detection)
    pin: str | None = None
    track: str | None = None

    def push_history(self, new_current: SkillVersion) -> None:
        """Promote new_current to current, pushing the old one onto capped history."""
        self.history.insert(0, self.current)
        self.current = new_current
        if len(self.history) > HISTORY_CAP:
            del self.history[HISTORY_CAP:]


class RenderRule(BaseModel):
    """Render-time view of a rule body, consumed by the AGENTS.md template
    (which references `.name`, `.description`, `.body`). Not persisted."""

    name: str
    body: str
    description: str | None = None


class Manifest(BaseModel):
    """Per-project lockfile recording all installed artifacts and pinned policy."""

    model_config = ConfigDict(extra="forbid")

    manifest_version: int = CURRENT_MANIFEST_VERSION
    instruction_template: str = "default"
    # Locked instruction archetype (repo-sourced AGENTS.md base). None = built-in.
    instruction_archetype: InstalledArchetype | None = None
    skills: list[InstalledSkill] = Field(default_factory=list)
    agents: list[InstalledAgent] = Field(default_factory=list)
    mcp_servers: list[InstalledMcpServer] = Field(default_factory=list)
    rules: list[InstalledRule] = Field(default_factory=list)
    managed_files: list[str] = Field(default_factory=list)
    # Hash of the last-written body of each managed region inside AGENTS.md (and
    # symlinks). Drift means the user edited inside markers — warn before rewrite.
    managed_region_hashes: dict[str, str] = Field(default_factory=dict)
    # Name of the active layout profile. None resolves to no profile.
    layout_profile: str | None = None
    # Explicit list of symlinks so sync can recreate them.
    symlinks: list[str] = Field(default_factory=list)
    # Governing policy pinned at lock time: org repo url (None for a local policy),
    # the resolved commit SHA (org only), and a content hash of the resolved policy.
    # Lets review/CI detect a disallowed/outdated policy.
    policy_repo: str | None = None
    policy_ref: str | None = None
    policy_hash: str | None = None
