"""Layout profiles — hackable directory-layout presets for agent tooling.

A layout profile controls where aim installs skills, rules, and mirror
files in a project. Profiles are TOML files under
`.aim/layout-profiles/<name>.toml` so they can be checked into a repo.

Scope:
- project: repo-only. Never cached in the DB.
- global: authoritative in the DB, with a read-only repo copy. If the repo copy
  is edited locally, sync demotes it to a project profile (repo wins).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlmodel import select

from aim.core import db, manifest, paths, policy
from aim.core.models import GlobalSetting
from aim.core.models import LayoutProfile as LayoutProfileRow
from aim.core.validation import is_valid_mirror_name

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RELATIVE_PATH_RE = re.compile(
    r"^(?:(?!\.{1,2}$)[A-Za-z0-9_\-\.]+)(?:/(?:(?!\.{1,2}$)[A-Za-z0-9_\-\.]+))*$"
)

_READ_ONLY_HEADER = (
    "# aim global layout profile — read-only copy.\n"
    "# Edits made here will demote this to a project-scoped profile.\n"
)

_DEFAULT_LAYOUT_PROFILE_KEY = "default_layout_profile"
_RESERVED_DIRS = frozenset((".git", ".hg", ".svn", ".bzr", "_darcs", ".pijul", ".sl", ".jj"))


class LayoutProfileNameError(ValueError):
    """Raised when a layout profile name is invalid or reserved."""


class LayoutProfileNotFoundError(KeyError):
    """Raised when a requested layout profile cannot be found."""


class LayoutProfileScope(StrEnum):
    """Scope of a layout profile: repo-only or globally cached."""

    PROJECT = "project"
    GLOBAL = "global"


class LayoutProfile(BaseModel):
    """Directory-layout preset controlling where aim installs artifacts."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    description: str | None = None
    scope: LayoutProfileScope = LayoutProfileScope.PROJECT
    rules_mode: Literal["files", "inline"] = "files"

    rules_dir: str = ".claude/rules"
    skills_dir: str = ".claude/skills"
    agents_dir: str = ".claude/agents"
    agents_md: str = "AGENTS.md"
    mcp_json: str = ".mcp.json"
    symlinks: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """Validate the profile name against the allowed character pattern.

        Args:
            value: Candidate profile name.

        Returns:
            The validated name unchanged.

        Raises:
            LayoutProfileNameError: If the name does not match the pattern.
        """
        if not _NAME_RE.fullmatch(value):
            raise LayoutProfileNameError(
                f"profile name {value!r} invalid: must match {_NAME_RE.pattern}"
            )
        return value

    @field_validator(
        "rules_dir",
        "skills_dir",
        "agents_dir",
        "agents_md",
        "mcp_json",
    )
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        """Validate that a path field is relative, descending-only, and safe.

        Args:
            value: Candidate relative path.

        Returns:
            The validated path unchanged.

        Raises:
            ValueError: If the path is empty, not relative, escapes upward, or
                names a reserved VCS directory.
        """
        if not value:
            raise ValueError("path must not be empty")
        if not _RELATIVE_PATH_RE.fullmatch(value):
            raise ValueError(f"path {value!r} invalid: must be a relative, descending-only path")
        for segment in value.split("/"):
            if segment in (".", ".."):
                raise ValueError(f"path {value!r} invalid: segments '.' and '..' are not allowed")
            if segment.lower() in _RESERVED_DIRS:
                raise ValueError(f"path {value!r} invalid: {segment!r} is a reserved directory")
        return value

    @field_validator("symlinks")
    @classmethod
    def _validate_symlink_names(cls, values: list[str]) -> list[str]:
        """Validate that every symlink entry is a legal mirror filename.

        Args:
            values: Candidate symlink filenames.

        Returns:
            The validated list unchanged.

        Raises:
            ValueError: If any filename is not a valid mirror name.
        """
        for value in values:
            if not is_valid_mirror_name(value):
                raise ValueError(f"filename {value!r} invalid")
        return values

    @model_validator(mode="after")
    def _agents_md_not_in_symlinks(self) -> LayoutProfile:
        """Ensure the agents_md target is not also listed as a symlink.

        Returns:
            The validated profile unchanged.

        Raises:
            ValueError: If agents_md also appears in symlinks.
        """
        if self.agents_md in self.symlinks:
            raise ValueError(f"agents_md {self.agents_md!r} must not also be listed in symlinks")
        return self


def _builtin(
    name: str,
    display_name: str,
    description: str,
    skills_dir: str,
    agents_dir: str,
    symlinks: list[str] | None = None,
    rules_mode: Literal["files", "inline"] = "files",
) -> LayoutProfile:
    """Construct a project-scoped built-in layout profile.

    Args:
        name: Stable identifier for the profile.
        display_name: Human-readable label.
        description: Short summary of the profile's behavior.
        skills_dir: Directory where skills are installed.
        agents_dir: Directory where agents are installed.
        symlinks: Mirror filenames to symlink to AGENTS.md.
        rules_mode: Whether rules are written as files or inlined.

    Returns:
        A configured LayoutProfile with built-in defaults.
    """
    return LayoutProfile(
        name=name,
        display_name=display_name,
        description=description,
        scope=LayoutProfileScope.PROJECT,
        rules_mode=rules_mode,
        rules_dir=".claude/rules",
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        agents_md="AGENTS.md",
        mcp_json=".mcp.json",
        symlinks=symlinks or [],
    )


BUILTIN_CLAUDE = _builtin(
    name="claude",
    display_name="Claude Code",
    description="Install skills under .claude/skills and symlink AGENTS.md as CLAUDE.md.",
    skills_dir=".claude/skills",
    agents_dir=".claude/agents",
    symlinks=["CLAUDE.md"],
    rules_mode="files",
)

BUILTIN_GEMINI = _builtin(
    name="gemini",
    display_name="Gemini CLI",
    description="Install skills under .gemini/skills and symlink AGENTS.md as GEMINI.md.",
    skills_dir=".gemini/skills",
    agents_dir=".gemini/agents",
    symlinks=["GEMINI.md"],
    rules_mode="inline",
)

_BUILTINS: dict[str, LayoutProfile] = {
    BUILTIN_CLAUDE.name: BUILTIN_CLAUDE,
    BUILTIN_GEMINI.name: BUILTIN_GEMINI,
}

_RESERVED_NAMES = frozenset(_BUILTINS.keys())


def _validate_not_reserved(name: str) -> None:
    """Reject names that collide with a built-in profile.

    Args:
        name: Profile name to check.

    Raises:
        LayoutProfileNameError: If the name is reserved.
    """
    if name in _RESERVED_NAMES:
        raise LayoutProfileNameError(f"profile name {name!r} is reserved for built-in profiles")


def content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of the given text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LayoutProfileTomlError(ValueError):
    """Raised when a layout profile's TOML cannot be parsed or validated."""


def parse_toml(text: str, *, source: str | None = None) -> LayoutProfile:
    """Parse and validate a layout profile from TOML text.

    Args:
        text: TOML source for the profile.
        source: Optional label used in error messages.

    Returns:
        The validated LayoutProfile.

    Raises:
        LayoutProfileTomlError: If the TOML is invalid or names a reserved name.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LayoutProfileTomlError(f"invalid TOML in {source or 'profile'}: {exc}") from exc
    # Drop fields removed from the model so profiles written by older aim versions
    # still parse: agent_dialect (pre-v2) and the plugin path fields now owned by
    # each plugin kind. Default missing rules_mode to "files" for the same reason.
    for _removed in ("agent_dialect", "plugins_dir", "opencode_plugins_dir", "claude_settings"):
        raw.pop(_removed, None)
    raw.setdefault("rules_mode", "files")
    # tomllib returns nested dicts as regular dicts; Pydantic handles them.
    try:
        return LayoutProfile.model_validate(raw)
    except LayoutProfileNameError as exc:
        # Reserved names are a parse-time concern too.
        raise LayoutProfileTomlError(str(exc)) from exc
    except ValidationError as exc:
        raise LayoutProfileTomlError(f"invalid {source or 'profile'}: {exc}") from exc


def render_toml(profile: LayoutProfile, *, read_only_copy: bool = False) -> str:
    """Render a layout profile to TOML text.

    Args:
        profile: Profile to serialize.
        read_only_copy: If True, prepend the read-only warning header.

    Returns:
        The TOML representation of the profile.
    """
    lines: list[str] = []
    if read_only_copy:
        lines.append(_READ_ONLY_HEADER)
    lines.append(f'name = "{profile.name}"')
    if profile.display_name:
        lines.append(f'display_name = "{_escape_toml_string(profile.display_name)}"')
    if profile.description:
        lines.append(f'description = "{_escape_toml_string(profile.description)}"')
    lines.append(f'scope = "{profile.scope.value}"')
    lines.append(f'rules_mode = "{profile.rules_mode}"')
    lines.append("")
    lines.append(f'rules_dir = "{profile.rules_dir}"')
    lines.append(f'skills_dir = "{profile.skills_dir}"')
    lines.append(f'agents_dir = "{profile.agents_dir}"')
    lines.append(f'agents_md = "{profile.agents_md}"')
    lines.append(f'mcp_json = "{profile.mcp_json}"')
    if profile.symlinks:
        lines.append(f"symlinks = {_render_string_list(profile.symlinks)}")
    lines.append("")
    return "\n".join(lines)


def _escape_toml_string(value: str) -> str:
    """Escape a string for safe inclusion in a TOML basic string."""

    def _replace(match: re.Match[str]) -> str:
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
    """Render a list of strings as a TOML inline array."""
    if not values:
        return "[]"
    parts = [f'"{_escape_toml_string(v)}"' for v in values]
    return "[" + ", ".join(parts) + "]"


def project_profile_dir(project_root: Path) -> Path:
    """Return the directory holding repo-side layout profiles."""
    return paths.project_layout_profiles_dir(project_root)


def project_profile_path(project_root: Path, name: str) -> Path:
    """Return the repo path for a named profile's TOML file.

    Args:
        project_root: Repository root.
        name: Profile name.

    Returns:
        Path to the profile's TOML file.

    Raises:
        LayoutProfileNameError: If the name is invalid.
    """
    if not _NAME_RE.fullmatch(name):
        raise LayoutProfileNameError(
            f"profile name {name!r} invalid: must match {_NAME_RE.pattern}"
        )
    return project_profile_dir(project_root) / f"{name}.toml"


def list_repo_profiles(project_root: Path) -> list[LayoutProfile]:
    """Return all parseable profiles stored in the repo, sorted by name.

    Args:
        project_root: Repository root.

    Returns:
        The repo profiles; corrupt files are skipped silently.
    """
    dir_ = project_profile_dir(project_root)
    if not dir_.exists():
        return []
    out: list[LayoutProfile] = []
    for path in sorted(dir_.glob("*.toml")):
        try:
            text = path.read_text(encoding="utf-8")
            profile = parse_toml(text, source=str(path))
            # Ensure name matches filename even if TOML says otherwise.
            profile = profile.model_copy(update={"name": path.stem})
            out.append(profile)
        except Exception:
            # Skip corrupt files; sync will surface warnings separately.
            continue
    return out


def _db_row_to_profile(row: Any) -> LayoutProfile:
    """Parse a DB cache row into a LayoutProfile."""
    return parse_toml(row.toml_text, source=f"db:{row.name}")


def list_db_profiles() -> list[LayoutProfile]:
    """Return all parseable profiles cached in the DB; corrupt rows skipped."""
    with db.session() as session:
        rows = list(session.exec(select(LayoutProfileRow)).all())
    out: list[LayoutProfile] = []
    for row in rows:
        try:
            out.append(_db_row_to_profile(row))
        except Exception:
            # Corrupt cached row; ignore.
            continue
    return out


def _upsert_db_profile(profile: LayoutProfile, text: str) -> None:
    """Insert or update the DB cache row for a profile.

    Args:
        profile: Profile whose name keys the row.
        text: Rendered TOML to store and hash.
    """
    h = content_hash(text)
    with db.session() as session:
        existing = session.get(LayoutProfileRow, profile.name)
        if existing is None:
            session.add(
                LayoutProfileRow(
                    name=profile.name,
                    content_hash=h,
                    toml_text=text,
                    updated_at=datetime.now(UTC),
                )
            )
        else:
            existing.content_hash = h
            existing.toml_text = text
            existing.updated_at = datetime.now(UTC)
        session.commit()


def _delete_db_profile(name: str) -> bool:
    """Delete a profile's DB cache row if present.

    Args:
        name: Profile name.

    Returns:
        True if a row was deleted, False if none existed.
    """
    with db.session() as session:
        row = session.get(LayoutProfileRow, name)
        if row is None:
            return False
        session.delete(row)
        session.commit()
    return True


def list_profiles(project_root: Path) -> list[LayoutProfile]:
    """Return built-ins plus repo and DB-cached profiles, deduplicated by name.

    Repo profiles override DB profiles of the same name. User-defined profiles
    override built-ins of the same name.

    Args:
        project_root: Repository root.

    Returns:
        The merged, name-unique set of available profiles.
    """
    by_name: dict[str, LayoutProfile] = dict(_BUILTINS)
    for p in list_db_profiles():
        by_name.setdefault(p.name, p)
    for p in list_repo_profiles(project_root):
        by_name[p.name] = p
    return list(by_name.values())


def get_profile(project_root: Path, name: str) -> LayoutProfile:
    """Get a single profile by name, with repo overriding DB and built-ins.

    Args:
        project_root: Repository root.
        name: Profile name to resolve.

    Returns:
        The resolved LayoutProfile.

    Raises:
        LayoutProfileNameError: If a repo or DB copy is corrupt.
        LayoutProfileNotFoundError: If no profile by that name exists.
    """
    # Repo takes precedence.
    repo_path = project_profile_path(project_root, name)
    if repo_path.exists():
        try:
            text = repo_path.read_text(encoding="utf-8")
            profile = parse_toml(text, source=str(repo_path))
            return profile.model_copy(update={"name": name})
        except Exception as exc:
            raise LayoutProfileNameError(f"corrupt repo profile {name!r}: {exc}") from exc
    # DB cache next.
    with db.session() as session:
        row = session.get(LayoutProfileRow, name)
    if row is not None:
        try:
            return _db_row_to_profile(row)
        except Exception as exc:
            raise LayoutProfileNameError(f"corrupt DB cached profile {name!r}: {exc}") from exc
    # Built-ins last.
    if name in _BUILTINS:
        return _BUILTINS[name]
    raise LayoutProfileNotFoundError(name)


def resolve_active(project_root: Path) -> LayoutProfile:
    """Return the active layout profile for a project.

    Falls back to the default Claude profile if no manifest exists or no
    profile is set.

    Args:
        project_root: Repository root.

    Returns:
        The active LayoutProfile, or the built-in Claude profile as fallback.
    """
    try:
        m = manifest.load(project_root)
    except manifest.ManifestNotFoundError:
        return BUILTIN_CLAUDE
    if m.layout_profile is None:
        return BUILTIN_CLAUDE
    try:
        return get_profile(project_root, m.layout_profile)
    except LayoutProfileNotFoundError:
        # Active profile name is recorded but not available. Preserve the name
        # in the manifest but return the default layout so the project keeps working.
        return BUILTIN_CLAUDE


def save_project_profile(project_root: Path, profile: LayoutProfile) -> Path:
    """Save a project-scoped profile to the repo only.

    Args:
        project_root: Repository root.
        profile: Profile to save; its scope is forced to project.

    Returns:
        Path to the written TOML file.
    """
    _validate_not_reserved(profile.name)
    profile = profile.model_copy(update={"scope": LayoutProfileScope.PROJECT})
    dir_ = project_profile_dir(project_root)
    dir_.mkdir(parents=True, exist_ok=True)
    path = project_profile_path(project_root, profile.name)
    text = render_toml(profile)
    path.write_text(text, encoding="utf-8")
    # Ensure no stale DB row exists under this name.
    _delete_db_profile(profile.name)
    return path


def save_global_profile(project_root: Path, profile: LayoutProfile) -> Path:
    """Save a global profile: authoritative in DB, read-only copy in repo.

    Args:
        project_root: Repository root.
        profile: Profile to save; its scope is forced to global.

    Returns:
        Path to the read-only repo copy.
    """
    _validate_not_reserved(profile.name)
    profile = profile.model_copy(update={"scope": LayoutProfileScope.GLOBAL})
    text = render_toml(profile)
    _upsert_db_profile(profile, text)
    dir_ = project_profile_dir(project_root)
    dir_.mkdir(parents=True, exist_ok=True)
    path = project_profile_path(project_root, profile.name)
    path.write_text(render_toml(profile, read_only_copy=True), encoding="utf-8")
    return path


def delete_project_profile(project_root: Path, name: str) -> bool:
    """Delete a project profile from the repo.

    Args:
        project_root: Repository root.
        name: Profile name.

    Returns:
        True if the file existed and was removed, False otherwise.

    Raises:
        LayoutProfileNameError: If the name is invalid.
    """
    if not _NAME_RE.fullmatch(name):
        raise LayoutProfileNameError(
            f"profile name {name!r} invalid: must match {_NAME_RE.pattern}"
        )
    path = project_profile_path(project_root, name)
    if not path.exists():
        return False
    path.unlink()
    return True


def delete_global_profile(project_root: Path, name: str) -> bool:
    """Delete a global profile: remove DB cache and repo read-only copy.

    Args:
        project_root: Repository root.
        name: Profile name.

    Returns:
        True if either the DB row or repo copy was removed.

    Raises:
        LayoutProfileNameError: If the name is invalid.
    """
    if not _NAME_RE.fullmatch(name):
        raise LayoutProfileNameError(
            f"profile name {name!r} invalid: must match {_NAME_RE.pattern}"
        )
    deleted_db = _delete_db_profile(name)
    deleted_repo = delete_project_profile(project_root, name)
    return deleted_db or deleted_repo


def set_active(project_root: Path, name: str) -> None:
    """Set the active layout profile for a project.

    Args:
        project_root: Repository root.
        name: Profile name to activate.

    Raises:
        LayoutProfileNotFoundError: If no profile by that name exists.
    """
    policy.assert_profile_allowed(policy.effective_policy(project_root), name)
    # Validate the profile exists.
    get_profile(project_root, name)
    m = manifest.load_or_default(project_root)
    m.layout_profile = name
    manifest.save(project_root, m)


def get_global_default() -> str | None:
    """Return the globally configured default profile name, if any."""
    with db.session() as session:
        row = session.get(GlobalSetting, _DEFAULT_LAYOUT_PROFILE_KEY)
    return row.value if row is not None else None


def set_global_default(name: str | None) -> None:
    """Set or clear the global default profile name.

    Args:
        name: Profile name to set, or None to clear the default.

    Raises:
        LayoutProfileNameError: If a non-None name is invalid.
    """
    if name is not None and not _NAME_RE.fullmatch(name):
        raise LayoutProfileNameError(
            f"profile name {name!r} invalid: must match {_NAME_RE.pattern}"
        )
    with db.session() as session:
        if name is None:
            row = session.get(GlobalSetting, _DEFAULT_LAYOUT_PROFILE_KEY)
            if row is not None:
                session.delete(row)
        else:
            row = session.get(GlobalSetting, _DEFAULT_LAYOUT_PROFILE_KEY)
            if row is None:
                session.add(GlobalSetting(key=_DEFAULT_LAYOUT_PROFILE_KEY, value=name))
            else:
                row.value = name
        session.commit()


@dataclass
class SyncReport:
    """Summary of changes made by a profile sync run."""

    upserted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sync_profiles(project_root: Path) -> SyncReport:
    """Sync repo profiles with the DB cache. Repo is authoritative.

    - Project profiles are repo-only; any DB row with the same name is removed.
    - Global profiles: if the repo copy matches the DB, update the DB. If the
      repo copy differs from the DB, demote to project scope in the repo and
      remove the DB row.

    Args:
        project_root: Repository root.

    Returns:
        A SyncReport listing upserted, demoted, removed names and warnings.
    """
    report = SyncReport()
    dir_ = project_profile_dir(project_root)

    repo_profiles: dict[str, LayoutProfile] = {}
    if dir_.exists():
        for path in sorted(dir_.glob("*.toml")):
            try:
                text = path.read_text(encoding="utf-8")
                profile = parse_toml(text, source=str(path))
                profile = profile.model_copy(update={"name": path.stem})
                repo_profiles[path.stem] = profile
            except Exception as exc:
                report.warnings.append(f"{path.name}: parse error — {exc}")

    with db.session() as session:
        db_rows: dict[str, Any] = {
            row.name: row for row in session.exec(select(LayoutProfileRow)).all()
        }

    for name, profile in repo_profiles.items():
        if profile.scope == LayoutProfileScope.PROJECT:
            if name in db_rows:
                _delete_db_profile(name)
                report.removed.append(name)
            continue

        # scope == global
        repo_text = render_toml(profile)
        repo_hash = content_hash(repo_text)
        db_row = db_rows.get(name)
        if db_row is None:
            _upsert_db_profile(profile, repo_text)
            report.upserted.append(name)
            continue

        if db_row.content_hash == repo_hash:
            # Repo matches DB; nothing to do (or DB could be newer if repo is
            # a read-only copy). In either case repo is authoritative and
            # matches, so no change.
            continue

        # Repo copy differs from DB. Demote to project scope in repo.
        demoted = profile.model_copy(update={"scope": LayoutProfileScope.PROJECT})
        demoted_text = render_toml(demoted)
        path = project_profile_path(project_root, name)
        path.write_text(demoted_text, encoding="utf-8")
        _delete_db_profile(name)
        report.demoted.append(name)

    return report
