"""Source-agnostic repo identity: committed files are byte-identical across machines.

A repo's identity is ``repo_id = sha256(normalize_repo_url(url))[:16]``. The CLI/TUI
stay alias-based, but the committed `aim.toml`/`aim.lock.toml` are translated to
id form at the serialization boundary, so two machines with different clone-URL
forms and different local aliases produce identical files.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aim.core import (
    db,
    declarations,
    init,
    install,
    lock,
    manifest,
    paths,
    plugin_install,
    policy,
    repos,
)
from aim.core.models import (
    DeclaredSkill,
    DeclaredTemplate,
    InstalledSkill,
    Manifest,
    ProjectDeclarations,
    SkillVersion,
)
from tests.fixtures import git_fixtures


def _skill_repo(tmp_path: Path, name: str = "src") -> Path:
    """Create a bare repo holding one skill `skills/foo/SKILL.md`; return its path."""
    working = git_fixtures.make_source_repo(
        tmp_path / name, files={"skills/foo/SKILL.md": "# foo\n\nFoo.\n"}
    )
    return git_fixtures.make_bare_remote(working, tmp_path / f"{name}.git")


def _switch_machine(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point global aim state at a fresh home dir, simulating a second machine."""
    monkeypatch.setenv("AIM_HOME", str(home))
    db.reset_engine()
    paths.ensure_global_dirs()
    db.reset_engine()


# --------------------------------------------------------------------------- #
# The headline gate: byte-identical committed files across machines.
# --------------------------------------------------------------------------- #
def test_committed_files_byte_identical_across_url_forms_and_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline gate: machine A locks; machine B — different clone-URL form AND
    a different local alias — reproduces the project via `sync` and its committed
    `aim.toml` + `aim.lock.toml` stay byte-identical to A's. No per-machine state
    (alias, raw URL) leaks onto disk, so the files are portable.

    Independent re-locks differ only in inherently-temporal fields (``installed_at``);
    the portability property is that `sync` of a committed lockfile rewrites nothing,
    which this asserts directly (sync writes if and only if a byte changes)."""
    from aim.core import sync

    # Bare dir without a `.git` suffix so a trailing-slash form normalizes equal.
    working = git_fixtures.make_source_repo(
        tmp_path / "work", files={"skills/foo/SKILL.md": "# foo\n\nFoo.\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "srcrepo")
    # Two clone-URL forms of the SAME repo that normalize identically (a trailing
    # slash) — both cloneable, standing in for ssh-vs-https on a real host.
    url_a = f"file://{bare}"
    url_b = f"file://{bare}/"
    assert policy.normalize_repo_url(url_a) == policy.normalize_repo_url(url_b)

    # Machine A: alias r1, URL form A. Install + lock + commit.
    home_a = tmp_path / "home_a"
    _switch_machine(monkeypatch, home_a)
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    repos.add("r1", url_a)
    init.run(init.InitOptions(project_root=proj_a))
    install.install(proj_a, "r1/foo")
    asyncio.run(lock.run(lock.LockOptions(project_root=proj_a)))
    toml_a = (proj_a / "aim.toml").read_bytes()
    lock_a = (proj_a / "aim.lock.toml").read_bytes()
    # No per-machine alias leaks onto disk.
    assert b"r1" not in toml_a and b"r1" not in lock_a

    # Machine B: fresh DB, alias r2, URL form B. Receives A's committed files and syncs.
    home_b = tmp_path / "home_b"
    _switch_machine(monkeypatch, home_b)
    repos.add("r2", url_b)
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / "aim.toml").write_bytes(toml_a)
    (proj_b / "aim.lock.toml").write_bytes(lock_a)
    asyncio.run(sync.run(sync.SyncOptions(project_root=proj_b, sync_agents=False)))

    # Byte-identical after B's sync — sync introduced no alias/URL churn.
    assert (proj_b / "aim.toml").read_bytes() == toml_a, "aim.toml must stay byte-identical"
    assert (proj_b / "aim.lock.toml").read_bytes() == lock_a, "lockfile must stay byte-identical"
    assert b"r2" not in (proj_b / "aim.toml").read_bytes()
    assert b"r2" not in (proj_b / "aim.lock.toml").read_bytes()


def test_serialization_identical_for_ssh_vs_https(tmp_path: Path) -> None:
    """The serialization boundary alone makes ssh and https forms identical on disk,
    with different local aliases — no cloning needed (pure `_to_disk`)."""
    ssh = "git@github.com:Org/Repo.git"
    https = "https://github.com/org/repo"
    assert policy.normalize_repo_url(ssh) == policy.normalize_repo_url(https)

    decl_a = ProjectDeclarations(
        repos={"r1": ssh},
        skills=[
            DeclaredSkill(
                qualified_name="r1/foo",
                repo_alias="r1",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            )
        ],
    )
    decl_b = ProjectDeclarations(
        repos={"other-alias": https},
        skills=[
            DeclaredSkill(
                qualified_name="other-alias/foo",
                repo_alias="other-alias",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            )
        ],
    )
    assert declarations._to_disk(decl_a) == declarations._to_disk(decl_b)

    ts = datetime(2026, 1, 1, tzinfo=UTC)  # fixed so only identity could differ

    def _installed(alias: str, url: str) -> Manifest:
        return Manifest(
            skills=[
                InstalledSkill(
                    qualified_name=f"{alias}/foo",
                    repo_alias=alias,
                    repo_url=url,
                    source_path="skills/foo",
                    target_dir=".claude/skills/foo",
                    current=SkillVersion(tag=None, sha="a" * 40, installed_at=ts),
                )
            ]
        )

    assert manifest._to_disk(_installed("r1", ssh)) == manifest._to_disk(_installed("z2", https))


# --------------------------------------------------------------------------- #
# A/B cross-alias dedup: B already has the URL under a different alias.
# --------------------------------------------------------------------------- #
def test_sync_resolves_under_local_alias_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B (URL already registered under a different alias) syncs A's lockfile: no
    duplicate clone dir / `[repos]` entry; artifacts resolve under B's alias."""
    from aim.core import sync

    bare = _skill_repo(tmp_path)
    url = f"file://{bare}"

    # Machine A authors a lockfile with alias "alpha".
    home_a = tmp_path / "home_a"
    _switch_machine(monkeypatch, home_a)
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    repos.add("alpha", url)
    init.run(init.InitOptions(project_root=proj_a))
    install.install(proj_a, "alpha/foo")
    asyncio.run(lock.run(lock.LockOptions(project_root=proj_a)))

    # Machine B already has the SAME repo under alias "beta". Copy A's committed
    # files into B's project and sync.
    home_b = tmp_path / "home_b"
    _switch_machine(monkeypatch, home_b)
    repos.add("beta", url)
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / "aim.toml").write_bytes((proj_a / "aim.toml").read_bytes())
    (proj_b / "aim.lock.toml").write_bytes((proj_a / "aim.lock.toml").read_bytes())

    asyncio.run(sync.run(sync.SyncOptions(project_root=proj_b)))

    # Only one repo registered (beta) — no duplicate clone under "alpha".
    assert [r.alias for r in repos.list_repos()] == ["beta"]
    assert not repos.clone_dir("alpha").exists()
    # Artifacts resolve under B's local alias in memory.
    m = manifest.load(proj_b)
    assert [s.qualified_name for s in m.skills] == ["beta/foo"]
    assert (proj_b / ".claude" / "skills" / "foo").exists()


# --------------------------------------------------------------------------- #
# Plugin portability: settings.json key + vendor path are id-based.
# --------------------------------------------------------------------------- #
def _marketplace_files() -> dict[str, str]:
    import json

    marketplace = {
        "name": "demo-market",
        "plugins": [{"name": "design-audit", "source": "./design-audit", "version": "1.0.0"}],
    }
    return {
        ".claude-plugin/marketplace.json": json.dumps(marketplace),
        "design-audit/.claude-plugin/plugin.json": json.dumps({"name": "design-audit"}),
        "design-audit/skills/audit/SKILL.md": "# audit\n",
    }


def test_plugin_surface_is_id_based_and_portable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A installs a plugin; B (different alias) reproduces identical committed
    `.claude/` files: the Claude-facing enablement key uses the upstream
    marketplace name, the vendor path stays id-based. Both are alias-independent,
    so the bytes match across machines."""
    import json

    working = git_fixtures.make_source_repo(tmp_path / "src", files=_marketplace_files())
    bare = git_fixtures.make_bare_remote(working, tmp_path / "src.git")
    url = f"file://{bare}"
    repo_id = policy.repo_id_for_url(url)
    expected_mkt = f"aim-{repo_id}"  # id-based vendor dir
    expected_key = f"demo-market-{repo_id[:8]}"  # upstream name + short id: the settings key

    from aim.core import sync

    # Machine A.
    home_a = tmp_path / "home_a"
    _switch_machine(monkeypatch, home_a)
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    repos.add("alpha", url)
    init.run(init.InitOptions(project_root=proj_a))
    plugin_install.install_plugin(proj_a, "alpha/design-audit")
    asyncio.run(lock.run(lock.LockOptions(project_root=proj_a)))

    settings_a = json.loads((proj_a / ".claude" / "settings.json").read_text())
    assert f"design-audit@{expected_key}" in settings_a["enabledPlugins"]
    assert expected_key in settings_a["extraKnownMarketplaces"]
    assert (proj_a / ".claude" / "plugins" / expected_mkt / "design-audit").exists()

    # Machine B with a different alias reproduces the SAME committed .claude bytes.
    home_b = tmp_path / "home_b"
    _switch_machine(monkeypatch, home_b)
    repos.add("zeta", url)
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / "aim.toml").write_bytes((proj_a / "aim.toml").read_bytes())
    (proj_b / "aim.lock.toml").write_bytes((proj_a / "aim.lock.toml").read_bytes())
    asyncio.run(sync.run(sync.SyncOptions(project_root=proj_b, sync_agents=False)))

    settings_b = json.loads((proj_b / ".claude" / "settings.json").read_text())
    assert settings_b["enabledPlugins"] == settings_a["enabledPlugins"]
    assert settings_b["extraKnownMarketplaces"] == settings_a["extraKnownMarketplaces"]
    assert (proj_b / ".claude" / "plugins" / expected_mkt / "design-audit").exists()


# --------------------------------------------------------------------------- #
# Round-trip: on-disk id-keyed, in-memory alias-keyed.
# --------------------------------------------------------------------------- #
def test_declarations_round_trip(home: Path, project_root: Path, tmp_path: Path) -> None:
    bare = _skill_repo(tmp_path)
    repos.add("a", f"file://{bare}")
    decl = ProjectDeclarations(
        repos={"a": f"file://{bare}"},
        skills=[
            DeclaredSkill(
                qualified_name="a/foo",
                repo_alias="a",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            )
        ],
    )
    declarations.save(project_root, decl)
    loaded = declarations.load(project_root)
    assert loaded.skills[0].qualified_name == "a/foo"
    assert loaded.skills[0].repo_alias == "a"
    assert "a" in loaded.repos
    # On disk the [repos] key is the repo_id, never the alias.
    raw = (project_root / "aim.toml").read_text()
    assert "a = " not in raw  # no alias key
    assert policy.repo_id_for_url(f"file://{bare}") in raw


def test_manifest_round_trip(home: Path, project_root: Path, tmp_path: Path) -> None:
    bare = _skill_repo(tmp_path)
    repos.add("a", f"file://{bare}")
    m = Manifest(
        skills=[
            InstalledSkill(
                qualified_name="a/foo",
                repo_alias="a",
                repo_url=f"file://{bare}",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
                current=SkillVersion(tag=None, sha="a" * 40, installed_at=datetime.now(UTC)),
            )
        ]
    )
    manifest.save(project_root, m)
    loaded = manifest.load(project_root)
    assert loaded.skills[0].qualified_name == "a/foo"
    assert loaded.skills[0].repo_alias == "a"
    assert loaded.skills[0].repo_url == f"file://{bare}"
    raw = (project_root / "aim.lock.toml").read_text()
    repo_id = policy.repo_id_for_url(f"file://{bare}")
    assert repo_id in raw  # [repos] section is id-keyed
    assert "repo_url" not in raw  # per-artifact repo_url dropped on disk


# --------------------------------------------------------------------------- #
# No-clone on load of an unregistered URL.
# --------------------------------------------------------------------------- #
def test_load_unregistered_url_does_not_clone(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """Loading committed files that reference an unregistered repo must NOT clone;
    it resolves to a default alias purely from the on-disk URL."""
    bare = _skill_repo(tmp_path)
    repos.add("a", f"file://{bare}")
    decl = ProjectDeclarations(
        repos={"a": f"file://{bare}"},
        skills=[
            DeclaredSkill(
                qualified_name="a/foo",
                repo_alias="a",
                source_path="skills/foo",
                target_dir=".claude/skills/foo",
            )
        ],
    )
    declarations.save(project_root, decl)
    repos.remove("a")  # now unregistered

    before = (
        set(p.name for p in paths.repos_cache_dir().glob("*"))
        if (paths.repos_cache_dir().exists())
        else set()
    )
    loaded = declarations.load(project_root)  # must not clone
    after = (
        set(p.name for p in paths.repos_cache_dir().glob("*"))
        if (paths.repos_cache_dir().exists())
        else set()
    )
    assert before == after, "load must not create any clone"
    # Resolves to a default owner-repo alias derived from the URL.
    expected = repos.derive_default_alias(f"file://{bare}")
    assert loaded.skills[0].repo_alias == expected


# --------------------------------------------------------------------------- #
# Migration of a current-version (v9 / v15) alias-keyed file -> deterministic id form.
# --------------------------------------------------------------------------- #
def test_declarations_migrate_v9_to_id_form(home: Path, project_root: Path) -> None:
    """A v9 alias-keyed aim.toml migrates to id form deterministically."""
    ssh = "git@github.com:Org/Repo.git"
    repo_id = policy.repo_id_for_url(ssh)
    v9 = (
        "manifest_version = 9\n"
        "\n"
        "[repos]\n"
        f'r1 = "{ssh}"\n'
        "\n"
        "[[skill]]\n"
        'qualified_name = "r1/foo"\n'
        'repo_alias = "r1"\n'
        'source_path = "skills/foo"\n'
        'target_dir = ".claude/skills/foo"\n'
    )
    (project_root / "aim.toml").write_text(v9)
    loaded = declarations.load(project_root)
    # In memory it resolves to a default alias (repo not registered), id form on disk.
    assert loaded.skills[0].qualified_name.endswith("/foo")
    # The raw migrated dict (pre-_from_disk) is id-keyed and matches a fresh write.
    import tomllib

    raw = tomllib.loads(v9)
    raw["skills"] = raw.pop("skill")
    migrated = declarations._migrate(raw)
    assert migrated["repos"] == {repo_id: policy.normalize_repo_url(ssh)}
    assert migrated["skills"][0]["repo_alias"] == repo_id
    assert migrated["skills"][0]["qualified_name"] == f"{repo_id}/foo"


def test_manifest_migrate_v15_to_id_form(home: Path, project_root: Path) -> None:
    """A v15 alias-keyed lockfile migrates to id form, dropping per-artifact repo_url
    and rewriting the plugin surface to ``aim-<repo_id>``."""
    ssh = "git@github.com:Org/Repo.git"
    repo_id = policy.repo_id_for_url(ssh)
    raw = {
        "manifest_version": 15,
        "skills": [
            {
                "qualified_name": "r1/foo",
                "repo_alias": "r1",
                "repo_url": ssh,
                "source_path": "skills/foo",
                "target_dir": ".claude/skills/foo",
                "current": {"tag": None, "sha": "a" * 40, "installed_at": "2026-01-01T00:00:00Z"},
            }
        ],
        "plugins": [
            {
                "qualified_name": "r1/design-audit",
                "repo_alias": "r1",
                "repo_url": ssh,
                "flavor": "claude",
                "source_path": "design-audit",
                "target_dir": ".claude/plugins/r1/design-audit",
                "marketplace_name": "r1",
                "current": {"tag": None, "sha": "b" * 40, "installed_at": "2026-01-01T00:00:00Z"},
            }
        ],
    }
    from aim.core.manifest_migrate import migrate

    migrated = migrate(raw)
    assert migrated["manifest_version"] == 16
    assert migrated["repos"] == {repo_id: policy.normalize_repo_url(ssh)}
    skill = migrated["skills"][0]
    assert skill["repo_alias"] == repo_id
    assert skill["qualified_name"] == f"{repo_id}/foo"
    assert "repo_url" not in skill
    plugin = migrated["plugins"][0]
    assert plugin["repo_alias"] == repo_id
    assert plugin["marketplace_name"] == f"aim-{repo_id}"
    assert plugin["target_dir"] == f".claude/plugins/aim-{repo_id}/design-audit"
    assert "repo_url" not in plugin


# --------------------------------------------------------------------------- #
# The registry-backed template repo also reaches disk — it must be id-form too.
# (The standalone org-policy repo is left verbatim: its URL is a direct clone
# target, so normalizing it would mutate what gets cloned. See changelog.)
# --------------------------------------------------------------------------- #
def test_template_repo_id_form_on_disk() -> None:
    """The registry-backed template repo gets id-form identity on disk (normalized URL,
    repo_id-prefixed qualified_name), so both committed files stay byte-identical across
    ssh/https forms."""
    ssh = "git@github.com:Org/Repo.git"
    https = "https://github.com/org/repo"
    repo_id = policy.repo_id_for_url(ssh)
    norm = policy.normalize_repo_url(ssh)
    assert norm == policy.normalize_repo_url(https)

    def _decl(url: str) -> ProjectDeclarations:
        return ProjectDeclarations(
            repos={"r1": url},
            template=DeclaredTemplate(qualified_name="r1/tmpl", repo_alias="r1", url=url),
        )

    disk = declarations._to_disk(_decl(ssh))
    assert disk == declarations._to_disk(_decl(https))
    assert disk["template"]["url"] == norm
    assert disk["template"]["repo_alias"] == repo_id
    assert disk["template"]["qualified_name"] == f"{repo_id}/tmpl"

    def _man(url: str) -> Manifest:
        return Manifest(template_repo=url, template_qualified_name="r1/tmpl")

    disk_man = manifest._to_disk(_man(ssh))
    assert disk_man == manifest._to_disk(_man(https))
    assert disk_man["template_repo"] == norm
    assert disk_man["template_qualified_name"] == f"{repo_id}/tmpl"


def test_template_round_trip_resolves_to_local_alias(
    home: Path, project_root: Path, tmp_path: Path
) -> None:
    """A registry-backed template serializes to id form but loads back under the local
    alias/URL, so `profile check/update` resolves it via the local index."""
    bare = _skill_repo(tmp_path)
    url = f"file://{bare}"
    repos.add("a", url)
    decl = ProjectDeclarations(
        repos={"a": url},
        template=DeclaredTemplate(qualified_name="a/tmpl", repo_alias="a", url=url),
    )
    declarations.save(project_root, decl)
    import tomllib

    on_disk = tomllib.loads((project_root / "aim.toml").read_text())
    repo_id = policy.repo_id_for_url(url)
    # Id-form on disk: the local alias never leaks; identity is the repo_id.
    assert on_disk["template"]["qualified_name"] == f"{repo_id}/tmpl"
    assert on_disk["template"]["repo_alias"] == repo_id

    loaded = declarations.load(project_root)
    assert loaded.template is not None
    assert loaded.template.qualified_name == "a/tmpl"  # resolved back to local alias
    assert loaded.template.repo_alias == "a"
    assert loaded.template.url == url  # local clone URL re-derived


# --------------------------------------------------------------------------- #
# The org-policy repo: normalized on disk for determinism, local form on load.
# --------------------------------------------------------------------------- #
def test_policy_repo_normalized_on_disk() -> None:
    """The org-policy repo URL is stored NORMALIZED in both committed files, so they stay
    byte-identical across ssh/https forms (the local clone form is re-derived on load)."""
    ssh = "git@github.com:Org/Repo.git"
    https = "https://github.com/org/repo"
    norm = policy.normalize_repo_url(ssh)

    def _decl(url: str) -> ProjectDeclarations:
        return ProjectDeclarations(policy={"scope": "org", "repo": url})

    assert declarations._to_disk(_decl(ssh)) == declarations._to_disk(_decl(https))
    assert declarations._to_disk(_decl(ssh))["policy"]["repo"] == norm

    def _man(url: str) -> Manifest:
        return Manifest(policy_repo=url)

    assert manifest._to_disk(_man(ssh)) == manifest._to_disk(_man(https))
    assert manifest._to_disk(_man(ssh))["policy_repo"] == norm


def test_policy_repo_round_trip_re_derives_local_url(home: Path, project_root: Path) -> None:
    """The committed policy URL is normalized, but loads back as THIS machine's recorded
    clone form — so refresh/fetch use the user's ssh/https URL, not the normalized one
    (which would mutate the clone target). `record_policy_repo_url` is what `bind` does."""
    import tomllib

    ssh = "git@github.com:Org/Repo.git"
    policy.record_policy_repo_url(ssh)

    declarations.save(project_root, ProjectDeclarations(policy={"scope": "org", "repo": ssh}))
    on_disk = tomllib.loads((project_root / "aim.toml").read_text())
    assert on_disk["policy"]["repo"] == policy.normalize_repo_url(ssh)  # normalized on disk
    assert declarations.load(project_root).policy["repo"] == ssh  # local form re-derived

    manifest.save(project_root, Manifest(policy_repo=ssh))
    on_disk_lock = tomllib.loads((project_root / "aim.lock.toml").read_text())
    assert on_disk_lock["policy_repo"] == policy.normalize_repo_url(ssh)
    assert manifest.load(project_root).policy_repo == ssh  # local form re-derived
