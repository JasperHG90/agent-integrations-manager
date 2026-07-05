"""Per-user aim preferences persisted in ``<user_config_dir>/config.toml``.

Holds preferences that outlive a session — currently the TUI's provenance
filter defaults. Deliberately NOT the ``GlobalSetting`` DB table: the DB is a
disposable cache, preferences are user state. The CLI does not read these —
CLI flags stay deterministic for scripting/CI; only TUI defaults persist.

Keys are addressed with dotted paths (e.g. ``tui.filters.show_plugin_owned``)
by `get`/`set_value`/`unset`. Unknown keys on disk are ignored on load
(forward compatibility with newer aim versions).
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from aim.core import paths

CONFIG_VERSION = 1


class UnknownConfigKeyError(KeyError):
    """The dotted key does not name a known config field."""


class InvalidConfigValueError(ValueError):
    """The raw value cannot be parsed/validated for the addressed field."""


class TuiFilterPrefs(BaseModel):
    """Provenance-filter defaults for the TUI artifact screens.

    Defaults mirror the core list functions: plugin-owned and `.claude/`-found
    are both shown by default.
    """

    model_config = ConfigDict(extra="ignore")

    show_plugin_owned: bool = True
    show_dot_claude: bool = True


class TuiPrefs(BaseModel):
    """TUI-scoped preferences."""

    model_config = ConfigDict(extra="ignore")

    filters: TuiFilterPrefs = Field(default_factory=TuiFilterPrefs)


class UserConfig(BaseModel):
    """The full persisted user configuration."""

    model_config = ConfigDict(extra="ignore")

    version: int = CONFIG_VERSION
    tui: TuiPrefs = Field(default_factory=TuiPrefs)


def config_path() -> Path:
    """Return the path of the user config file (may not exist yet)."""
    return paths.user_config_dir() / "config.toml"


def load() -> UserConfig:
    """Load the user config, returning defaults when absent or unreadable.

    A corrupt file yields defaults rather than an error: preferences are
    non-critical, and the next `save` rewrites a valid file.
    """
    path = config_path()
    if not path.exists():
        return UserConfig()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return UserConfig()
    try:
        return UserConfig.model_validate(raw)
    except Exception:
        return UserConfig()


def save(cfg: UserConfig) -> Path:
    """Atomically write the user config file, creating its directory."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = tomli_w.dumps(cfg.model_dump(mode="json"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


# Top-level fields that are schema metadata, not user-addressable preferences.
_RESERVED_KEYS = {"version"}


def _walk(cfg: UserConfig, key: str) -> tuple[BaseModel, str]:
    """Resolve a dotted key to its parent model and final field name.

    Raises:
        UnknownConfigKeyError: A segment does not name a known field, the path
            stops on a nested model instead of a leaf value, or the key is
            schema metadata (e.g. ``version``) rather than a preference.
    """
    if key in _RESERVED_KEYS:
        raise UnknownConfigKeyError(key)
    parts = key.split(".")
    node: BaseModel = cfg
    for i, part in enumerate(parts[:-1]):
        if part not in type(node).model_fields:
            raise UnknownConfigKeyError(".".join(parts[: i + 1]))
        value = getattr(node, part)
        if not isinstance(value, BaseModel):
            raise UnknownConfigKeyError(key)
        node = value
    leaf = parts[-1]
    if leaf not in type(node).model_fields:
        raise UnknownConfigKeyError(key)
    if isinstance(getattr(node, leaf), BaseModel):
        raise UnknownConfigKeyError(key)  # dotted path must address a leaf value
    return node, leaf


def get(key: str) -> object:
    """Return the value at a dotted key from the persisted config."""
    node, leaf = _walk(load(), key)
    return getattr(node, leaf)


def _parse_raw(current: object, raw: str, key: str) -> object:
    """Parse a raw CLI string into the type of the current value."""
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise InvalidConfigValueError(f"{key}: expected a boolean, got {raw!r}")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise InvalidConfigValueError(f"{key}: expected an integer, got {raw!r}") from exc
    return raw


def set_value(key: str, raw: str) -> UserConfig:
    """Set a dotted key from a raw string value, validate, persist, and return."""
    cfg = load()
    node, leaf = _walk(cfg, key)
    setattr(node, leaf, _parse_raw(getattr(node, leaf), raw, key))
    cfg = UserConfig.model_validate(cfg.model_dump())  # re-validate the whole tree
    save(cfg)
    return cfg


def unset(key: str) -> UserConfig:
    """Reset a dotted key to its default value, persist, and return."""
    cfg = load()
    node, leaf = _walk(cfg, key)
    default_node, _ = _walk(UserConfig(), key)
    setattr(node, leaf, getattr(default_node, leaf))
    save(cfg)
    return cfg


def list_values() -> list[tuple[str, object, object]]:
    """Return ``(dotted key, current value, default value)`` for every leaf."""
    out: list[tuple[str, object, object]] = []

    def _collect(node: BaseModel, defaults: BaseModel, prefix: str) -> None:
        for name in type(node).model_fields:
            dotted = f"{prefix}{name}"
            if dotted in _RESERVED_KEYS:
                continue  # schema metadata, not a user preference
            value = getattr(node, name)
            default = getattr(defaults, name)
            if isinstance(value, BaseModel):
                _collect(value, default, f"{dotted}.")
            else:
                out.append((dotted, value, default))

    _collect(load(), UserConfig(), "")
    return out
