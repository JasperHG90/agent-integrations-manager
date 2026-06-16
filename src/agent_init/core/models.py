"""Single set of pydantic models. SQLModel tables are thin persistence shells over them.

Per the plan: one model layer to avoid drift between DB and JSON shapes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

# ---------- DB tables ----------


class RegisteredRepo(SQLModel, table=True):
    """A skill source repo registered globally on this machine."""

    alias: str = SQLField(primary_key=True)
    url: str
    default_ref: str = "HEAD"  # branch/tag to track on refresh
    last_fetched_at: datetime | None = None
    last_sha: str | None = None


class SkillIndex(SQLModel, table=True):
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


class Template(SQLModel, table=True):
    """A registered AGENTS.md template (built-in default plus user-registered)."""

    name: str = SQLField(primary_key=True)
    source: str  # "builtin", or a path / url for user-registered
    description: str | None = None


class RuleEntry(SQLModel, table=True):
    """User-saved rule snippet. Body lives at user_config_dir/rules/<name>.md."""

    name: str = SQLField(primary_key=True)
    is_default: bool = False
    description: str | None = None


class RegisteredRuleRepo(SQLModel, table=True):
    """A shared rule library overlay — markdown rules cloned from a git repo
    and resolved as a lower-priority source after the local library."""

    alias: str = SQLField(primary_key=True)
    url: str
    default_ref: str = "HEAD"
    last_fetched_at: datetime | None = None
    last_sha: str | None = None


class LayoutProfile(SQLModel, table=True):
    """Cached global layout profile. Repo-side global profiles are authoritative;
    the DB is a cache for sharing across projects."""

    name: str = SQLField(primary_key=True)
    content_hash: str
    toml_text: str
    updated_at: datetime


class GlobalSetting(SQLModel, table=True):
    """Single-row key/value settings for machine-wide defaults."""

    key: str = SQLField(primary_key=True)
    value: str


class AgentIndex(SQLModel, table=True):
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


class RuleIndex(SQLModel, table=True):
    """Discovered rule within a registered repo, used for rule search/overlay."""

    qualified_name: str = SQLField(primary_key=True)  # "<alias>/<rule_name>"
    repo_alias: str = SQLField(index=True)
    rule_name: str = SQLField(index=True)
    rule_md_path: str  # path of the .md file relative to repo root
    title: str | None = None
    description: str | None = None
    indexed_at_sha: str


class McpServerCache(SQLModel, table=True):
    """Cached default MCP registry server definitions for instant TUI startup."""

    name: str = SQLField(primary_key=True)  # canonical registry server name
    definition_json: str  # raw registry server JSON (by_alias dump)
    fetched_at: datetime


# ---------- Manifest (per-project JSON, committed) ----------

CURRENT_MANIFEST_VERSION = 4  # v4: mcp_servers and agents lists (additive)
HISTORY_CAP = 10


class SkillVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str | None = None
    sha: str
    installed_at: datetime

    def identifier(self) -> str:
        """User-facing composite identifier per plan: `<tag>+<short_sha>` or SHA-only."""
        short = self.sha[:7]
        return f"{self.tag}+{short}" if self.tag else short


class InstalledSkill(BaseModel):
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
    model_config = ConfigDict(extra="forbid")

    definition_hash: str  # sha256 of canonical registry definition JSON
    registry_version: str | None = None
    installed_at: datetime
    entry: McpClaudeEntry | None = None  # historical .mcp.json entry for rollback

    def identifier(self) -> str:
        return self.registry_version or self.definition_hash[:7]


class InstalledMcpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str  # local project alias (user-editable)
    registry_name: str  # canonical registry server name
    entry: McpClaudeEntry  # exact .mcp.json entry written
    entry_hash: str  # sha256 of canonical entry JSON
    current: McpServerVersion
    history: list[McpServerVersion] = Field(default_factory=list)

    def push_history(self, new_current: McpServerVersion) -> None:
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
        self.history.insert(0, self.current)
        self.current = new_current
        if len(self.history) > HISTORY_CAP:
            del self.history[HISTORY_CAP:]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: int = CURRENT_MANIFEST_VERSION
    template: str = "default"
    skills: list[InstalledSkill] = Field(default_factory=list)
    agents: list[InstalledAgent] = Field(default_factory=list)
    mcp_servers: list[InstalledMcpServer] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    managed_files: list[str] = Field(default_factory=lambda: ["AGENTS.md"])
    # Hash of the last-written body of each managed region inside AGENTS.md (and
    # mirrors). Drift means the user edited inside markers — warn before rewrite.
    managed_region_hashes: dict[str, str] = Field(default_factory=dict)
    # Per-project preference for the primary agent dialect ("claude", "gemini",
    # "opencode", or None). Not used for rendering yet — laid down for future
    # per-agent dialect support without another manifest version bump.
    agent_dialect: str | None = None
    # Name of the active layout profile. None resolves to the legacy profile
    # with the original hardcoded paths and no default mirrors.
    layout_profile: str | None = None
