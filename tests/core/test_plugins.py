from __future__ import annotations

import json
from pathlib import Path

import pytest

from aim.core import paths, plugins, repos
from tests.fixtures import git_fixtures

# An external (pluggable) opencode kind — deliberately NOT a built-in. Tests drop
# it into the global targets dir to prove a new client can be added without an aim
# source change.
OPENCODE_KIND_TOML = """
name = "opencode"
[manifest]
file = "package.json"
name = "name"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def _install_opencode_kind() -> None:
    """Drop the external opencode kind into the global targets dir (AIM_HOME-isolated)."""
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "opencode.toml").write_text(OPENCODE_KIND_TOML)


def _claude_marketplace_files(extra: dict[str, str] | None = None) -> dict[str, str]:
    marketplace = {
        "name": "demo-market",
        "owner": {"name": "demo", "url": "https://example.com"},
        "description": "demo marketplace",
        "plugins": [
            {
                "name": "design-audit",
                "source": "./design-audit",
                "description": "audit",
                "version": "1.0.0",
                "category": "design",
                "keywords": ["ux"],
            },
            {
                "name": "typography",
                "source": "./typography",
                "description": "type",
                "version": "2.0",
            },
        ],
    }
    files = {
        ".claude-plugin/marketplace.json": json.dumps(marketplace),
        "design-audit/.claude-plugin/plugin.json": json.dumps(
            {"name": "design-audit", "version": "1.0.0"}
        ),
        "design-audit/skills/audit/SKILL.md": "# audit\n",
        "typography/.claude-plugin/plugin.json": json.dumps({"name": "typography"}),
    }
    if extra:
        files.update(extra)
    return files


def _build(tmp_path: Path, files: dict[str, str]) -> Path:
    working = git_fixtures.make_source_repo(tmp_path / "src", files=files)
    return git_fixtures.make_bare_remote(working, tmp_path / "bare.git")


def test_index_discovers_marketplace_and_plugins(home: Path, tmp_path: Path) -> None:
    bare = _build(tmp_path, _claude_marketplace_files())
    repos.add("a", f"file://{bare}")

    markets = plugins.list_marketplaces()
    assert [m.marketplace_name for m in markets] == ["demo-market"]
    assert markets[0].owner_name == "demo"

    rows = plugins.list_plugins()
    names = sorted(r.plugin_name for r in rows)
    assert names == ["design-audit", "typography"]
    audit = next(r for r in rows if r.plugin_name == "design-audit")
    assert audit.flavor == "claude"
    assert audit.marketplace_name == "demo-market"
    assert audit.source_path == "design-audit"
    assert audit.version == "1.0.0"


def test_marketplace_only_repo_registers(home: Path, tmp_path: Path) -> None:
    # A repo containing only a marketplace (no skills/agents/rules) must register.
    bare = _build(tmp_path, _claude_marketplace_files())
    repos.add("a", f"file://{bare}")  # must not raise RepoHasNoArtifactsError
    assert "plugin" in repos.artifact_kinds("a")


def test_list_filters(home: Path, tmp_path: Path) -> None:
    bare = _build(tmp_path, _claude_marketplace_files())
    repos.add("a", f"file://{bare}")
    assert len(plugins.list_plugins(flavor="claude")) == 2
    assert plugins.list_plugins(flavor="opencode") == []
    assert len(plugins.list_plugins(marketplace="demo-market")) == 2
    assert plugins.list_plugins(marketplace="nope") == []


def test_opencode_is_a_pluggable_kind(home: Path, tmp_path: Path) -> None:
    bare = _build(
        tmp_path,
        {
            "logger/package.json": json.dumps({"name": "logger"}),
            "logger/index.ts": "export const plugin = async () => ({})\n",
        },
    )
    # opencode is NOT a built-in kind — an opencode-only repo has nothing aim
    # recognizes, so registration is rejected.
    with pytest.raises(repos.RepoHasNoArtifactsError):
        repos.add("a", f"file://{bare}")

    # Drop the external opencode kind spec → the same repo now exposes the plugin,
    # named by its package.json and rooted at the package directory.
    _install_opencode_kind()
    repos.add("a", f"file://{bare}")
    rows = plugins.list_plugins(flavor="opencode")
    assert [r.plugin_name for r in rows] == ["logger"]
    assert rows[0].source_path == "logger"
    assert rows[0].marketplace_name is None


_OPENCODE_KIND_WITH_DESCRIPTION = """
name = "opencode"
[manifest]
file = "package.json"
name = "name"
description = "description"
[register]
vendor_into = ".opencode/plugins/{name}"
"""


def test_kind_description_keypath_populates_index(home: Path, tmp_path: Path) -> None:
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "opencode.toml").write_text(_OPENCODE_KIND_WITH_DESCRIPTION)
    bare = _build(
        tmp_path,
        {"logger/package.json": json.dumps({"name": "logger", "description": "logs things"})},
    )
    repos.add("a", f"file://{bare}")
    rows = plugins.list_plugins(flavor="opencode")
    assert [(r.plugin_name, r.description) for r in rows] == [("logger", "logs things")]


def test_kind_without_description_keypath_leaves_it_unset(home: Path, tmp_path: Path) -> None:
    # The default opencode kind declares no description keypath, so even a manifest
    # that has a description field must not populate it (opt-in behavior).
    _install_opencode_kind()
    bare = _build(
        tmp_path,
        {"logger/package.json": json.dumps({"name": "logger", "description": "ignored"})},
    )
    repos.add("a", f"file://{bare}")
    rows = plugins.list_plugins(flavor="opencode")
    assert rows[0].description is None


def test_remote_and_unsafe_sources_skipped(home: Path, tmp_path: Path) -> None:
    marketplace = {
        "name": "demo",
        "plugins": [
            {"name": "local-ok", "source": "./local-ok"},
            {"name": "remote", "source": {"source": "github", "repo": "x/y"}},
            {"name": "escape", "source": "../escape"},
            {"name": "abs", "source": "/etc/passwd"},
        ],
    }
    bare = _build(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "local-ok/.claude-plugin/plugin.json": json.dumps({"name": "local-ok"}),
        },
    )
    repos.add("a", f"file://{bare}")
    # The marketplace still registers; only the local-relative plugin is indexed.
    assert [r.plugin_name for r in plugins.list_plugins()] == ["local-ok"]
    warnings = plugins.take_skipped_warnings()
    assert any("remote" in w for w in warnings)
    assert any("escape" in w for w in warnings)
    assert any("abs" in w for w in warnings)  # absolute source rejected, not rewritten


def test_search_matches_description(home: Path, tmp_path: Path) -> None:
    bare = _build(tmp_path, _claude_marketplace_files())
    repos.add("a", f"file://{bare}")
    assert [r.plugin_name for r in plugins.search("audit")] == ["design-audit"]


def test_same_name_same_target_shadowed(home: Path, tmp_path: Path) -> None:
    # Two plugins with the same name AND same kind collide; the shallower path
    # wins (per _rank) and the other is shadowed with a warning. (Same name under
    # DIFFERENT kinds is the coexistence case, tested in test_plugin_install.)
    marketplace = {
        "name": "demo",
        "plugins": [
            {"name": "dup", "source": "./nested/dup"},
            {"name": "dup", "source": "./dup"},
        ],
    }
    bare = _build(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "nested/dup/.claude-plugin/plugin.json": json.dumps({"name": "dup"}),
            "dup/.claude-plugin/plugin.json": json.dumps({"name": "dup"}),
        },
    )
    repos.add("a", f"file://{bare}")
    rows = [r for r in plugins.list_plugins() if r.plugin_name == "dup"]
    assert len(rows) == 1  # collapsed to one
    assert rows[0].source_path == "dup"  # shallower path wins
    assert any("shadowed" in w for w in plugins.take_skipped_warnings())


def test_plugin_bundled_artifacts_not_indexed(home: Path, tmp_path: Path) -> None:
    # A skill bundled inside a plugin's dir must not surface as a standalone skill.
    from aim.core import skills

    marketplace = {"name": "demo", "plugins": [{"name": "bundler", "source": "./bundler"}]}
    bare = _build(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "bundler/.claude-plugin/plugin.json": json.dumps({"name": "bundler"}),
            "bundler/skills/inner/SKILL.md": "# inner\n",
            "skills/standalone/SKILL.md": "# standalone\n",
        },
    )
    repos.add("a", f"file://{bare}")
    names = {r.skill_name for r in skills.list_skills()}
    assert "standalone" in names
    assert "inner" not in names  # bundled in the 'bundler' plugin


def test_repo_root_plugin_discovered(home: Path, tmp_path: Path) -> None:
    # A single-plugin repo (the superpowers shape): a root marketplace whose only
    # plugin has source "./", i.e. the whole repo IS the plugin. It must index, and
    # its bundled skills must NOT surface standalone.
    from aim.core import skills

    marketplace = {
        "name": "superpowers-dev",
        "plugins": [{"name": "superpowers", "source": "./", "version": "6.0.3"}],
    }
    bare = _build(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            ".claude-plugin/plugin.json": json.dumps({"name": "superpowers", "version": "6.0.3"}),
            "skills/tdd/SKILL.md": "# tdd\n",
        },
    )
    repos.add("a", f"file://{bare}")

    rows = plugins.list_plugins()
    assert [r.plugin_name for r in rows] == ["superpowers"]
    assert rows[0].source_path == ""  # repo root
    assert rows[0].version == "6.0.3"
    assert rows[0].marketplace_name == "superpowers-dev"
    # The repo's own skill is bundled in the whole-repo plugin, not standalone.
    assert "tdd" not in {r.skill_name for r in skills.list_skills()}


def test_plugin_version_from_plugin_json(home: Path, tmp_path: Path) -> None:
    # The plugin's own plugin.json version is the source of truth; the marketplace
    # entry's version is only a fallback.
    marketplace = {
        "name": "demo",
        "plugins": [
            {"name": "withpj", "source": "./withpj", "version": "9.9.9"},
            {"name": "nopjver", "source": "./nopjver", "version": "0.1.0"},
        ],
    }
    bare = _build(
        tmp_path,
        {
            ".claude-plugin/marketplace.json": json.dumps(marketplace),
            "withpj/.claude-plugin/plugin.json": json.dumps({"name": "withpj", "version": "1.2.3"}),
            "nopjver/.claude-plugin/plugin.json": json.dumps({"name": "nopjver"}),
        },
    )
    repos.add("a", f"file://{bare}")
    versions = {r.plugin_name: r.version for r in plugins.list_plugins()}
    assert versions["withpj"] == "1.2.3"  # plugin.json wins over the marketplace entry
    assert versions["nopjver"] == "0.1.0"  # falls back to the marketplace entry version


# A PROJECT-scoped target (`.aim/targets/`), as opposed to the global one above.
GEMINI_TARGET_TOML = """
name = "gemini"
[manifest]
file = "gemini-extension.json"
name = "name"
[register]
vendor_into = ".gemini/extensions/{name}"
"""


def test_invalid_target_spec_surfaces_warning(home: Path, tmp_path: Path) -> None:
    # A stale spec written for the old schema (`[discover]`/`name_from`) no longer
    # validates. It must be skipped WITH a warning naming the file, not vanish.
    bare = _build(tmp_path, _claude_marketplace_files())
    d = paths.user_config_dir() / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "stale.toml").write_text(
        'name = "stale"\n[discover]\nmanifest = ["x"]\nname_from = "stem"\n'
        '[register]\nvendor_into = ".x/{name}"\nvendor_as = "file"\n'
    )
    repos.add("a", f"file://{bare}")  # triggers discovery → loads kinds
    warnings = plugins.take_skipped_warnings()
    assert any("stale.toml" in w for w in warnings)


def test_project_scoped_target_discovered_by_list(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """A target spec dropped in the PROJECT `.aim/targets/` (not the global dir) must let
    `plugin list` discover that client's plugins in registered repos. Indexing is
    machine-global, so the project target is honored via a live overlay at list time."""
    bare = _build(tmp_path, {"weather/gemini-extension.json": json.dumps({"name": "weather"})})
    # The repo is registered with global kinds only — gemini is project-scoped, so the
    # global index sees no plugin here (allow_empty keeps the registration).
    repos.add("a", f"file://{bare}", allow_empty=True)
    assert plugins.list_plugins(flavor="gemini") == []  # not in the global index

    targets = project_root / ".aim" / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / "gemini.toml").write_text(GEMINI_TARGET_TOML)

    rows = plugins.list_plugins(flavor="gemini", project_root=project_root)
    assert [r.plugin_name for r in rows] == ["weather"]  # name from the manifest
    assert rows[0].source_path == "weather"  # the plugin directory
    assert rows[0].flavor == "gemini"
    assert rows[0].qualified_name == "a/weather"
