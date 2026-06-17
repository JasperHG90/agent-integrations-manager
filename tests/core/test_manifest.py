from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aim.core import manifest as manifest_mod
from aim.core.manifest_migrate import ManifestVersionError, migrate
from aim.core.models import (
    CURRENT_MANIFEST_VERSION,
    HISTORY_CAP,
    InstalledSkill,
    Manifest,
    SkillVersion,
)


def _skill(tag: str | None, sha: str) -> InstalledSkill:
    return InstalledSkill(
        qualified_name="anthropic/code-review",
        repo_alias="anthropic",
        repo_url="https://github.com/anthropics/skills",
        source_path="skills/code-review",
        target_dir=".claude/skills/code-review",
        current=SkillVersion(tag=tag, sha=sha, installed_at=datetime.now(UTC)),
    )


def test_load_or_default_returns_empty_when_no_manifest(project_root: Path) -> None:
    m = manifest_mod.load_or_default(project_root)
    assert m.manifest_version == CURRENT_MANIFEST_VERSION
    assert m.skills == []
    assert m.rules == []


def test_load_raises_when_missing(project_root: Path) -> None:
    with pytest.raises(manifest_mod.ManifestNotFoundError):
        manifest_mod.load(project_root)


def test_save_then_load_round_trip(project_root: Path) -> None:
    m = Manifest(skills=[_skill("v1.0.0", "abcdef1234")])
    manifest_mod.save(project_root, m)
    loaded = manifest_mod.load(project_root)
    assert loaded.skills[0].current.tag == "v1.0.0"
    assert loaded.skills[0].current.identifier() == "v1.0.0+abcdef1"


def test_identifier_sha_only(project_root: Path) -> None:
    sv = SkillVersion(tag=None, sha="ab12cd3e9f8a", installed_at=datetime.now(UTC))
    assert sv.identifier() == "ab12cd3"


def test_history_push_caps_at_limit() -> None:
    skill = _skill("v1.0.0", "aaaaaaa")
    for i in range(HISTORY_CAP + 5):
        new = SkillVersion(tag=f"v1.0.{i + 1}", sha=f"{i:07d}", installed_at=datetime.now(UTC))
        skill.push_history(new)
    assert len(skill.history) == HISTORY_CAP
    # newest history entry should be the previously-current one
    assert skill.history[0].tag.startswith("v1.0.")


def test_migrate_from_v0() -> None:
    raw = {"instruction_template": "default", "skills": [], "rules": []}
    out = migrate(raw)
    assert out["manifest_version"] == CURRENT_MANIFEST_VERSION


def test_migrate_rejects_future_version() -> None:
    with pytest.raises(ManifestVersionError):
        migrate({"manifest_version": 999})


def test_manifest_rejects_unknown_fields(project_root: Path) -> None:
    bad = {"manifest_version": 1, "skills": [], "rules": [], "garbage": True}
    path = project_root / ".atm" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bad))
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        manifest_mod.load(project_root)
