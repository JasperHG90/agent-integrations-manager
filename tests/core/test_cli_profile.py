"""CLI coverage for `aim template` (and the back-compat `aim profile` alias)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aim import cli
from aim.core import profiles, repos
from tests.fixtures import git_fixtures

_runner = CliRunner()

_V1 = """
name = "svc"
instruction_template = "default"

[[skill]]
qualified_name = "src/foo"
"""

_V2 = """
name = "svc"
instruction_template = "default"

[[skill]]
qualified_name = "src/foo"

[[skill]]
qualified_name = "src/bar"
"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI color codes so assertions survive colorized output."""
    return _ANSI_RE.sub("", text)


def test_export_then_import_round_trips(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src", files={"skills/foo/SKILL.md": "# foo\n"}
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    url = f"file://{bare}"
    repos.add("acme", url)
    # Saved with no [[repo]] block / SHAs (as the old builder produced); export
    # must resolve them from the local index so the shared TOML is reconstructable.
    profiles.save(
        profiles.Profile(
            name="svc",
            description="service template",
            skills=[profiles.ProfileSkill(qualified_name="acme/foo")],
        )
    )
    out = tmp_path / "svc.toml"

    res = _runner.invoke(cli.app, ["template", "export", "svc", str(out)])
    assert res.exit_code == 0, _plain(res.output)
    assert out.exists()
    exported = out.read_text(encoding="utf-8")
    assert "[[repo]]" in exported
    assert f'url = "{url}"' in exported

    res = _runner.invoke(cli.app, ["template", "import", str(out), "--name", "svc2"])
    assert res.exit_code == 0, _plain(res.output)

    imported = profiles.load("svc2")
    assert imported.name == "svc2"
    assert imported.description == "service template"
    assert imported.repos == [profiles.ProfileRepo(alias="acme", url=url)]
    assert [s.qualified_name for s in imported.skills] == ["acme/foo"]
    assert imported.skills[0].sha  # frozen at export


def test_import_invalid_toml_is_friendly(home: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("name: not-toml\n", encoding="utf-8")
    res = _runner.invoke(cli.app, ["template", "import", str(bad)])
    assert res.exit_code == 1
    assert "error:" in _plain(res.output)


def test_check_exit_codes(home: Path, project_root: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n", "templates/svc.toml": _V1},
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("src", f"file://{bare}")
    profiles.apply("src/svc", project_root)

    res = _runner.invoke(cli.app, ["template", "check", str(project_root), "--json"])
    assert res.exit_code == 0, _plain(res.output)
    payload = json.loads(res.output)
    assert payload["has_template"] is True
    assert payload["up_to_date"] is True

    # Advance the template upstream → check exits 2 (template drift).
    git_fixtures.add_commit(
        working, {"templates/svc.toml": _V2, "skills/bar/SKILL.md": "# b\n"}, "v2"
    )
    subprocess.run(
        ["git", "-C", str(working), "push", "--quiet", str(bare), "main"],
        check=True,
        capture_output=True,
    )
    repos.refresh("src")
    res = _runner.invoke(cli.app, ["template", "check", str(project_root)])
    assert res.exit_code == 2, _plain(res.output)


def test_check_no_template_exits_zero(home: Path, project_root: Path) -> None:
    from aim.core import init as init_mod

    init_mod.run(init_mod.InitOptions(project_root=project_root))
    res = _runner.invoke(cli.app, ["template", "check", str(project_root)])
    assert res.exit_code == 0, _plain(res.output)
    assert "not stamped" in _plain(res.output)


def test_profile_alias_still_dispatches(home: Path) -> None:
    profiles.save(profiles.Profile(name="aliased"))
    res = _runner.invoke(cli.app, ["profile", "list"])
    assert res.exit_code == 0, _plain(res.output)
    assert "aliased" in _plain(res.output)


def test_list_includes_repo_templates_without_flag(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={
            "skills/foo/SKILL.md": "# foo\n",
            "templates/svc.toml": 'name = "svc"\n[[skill]]\nqualified_name = "src/foo"\n',
        },
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("src", f"file://{bare}")

    # The plain `template list` must surface repo-hosted templates, not just saved.
    res = _runner.invoke(cli.app, ["template", "list"])
    assert res.exit_code == 0, _plain(res.output)
    assert "src/svc" in _plain(res.output)


def test_list_bad_repo_alias_errors_with_hint(home: Path) -> None:
    res = _runner.invoke(cli.app, ["template", "list", "--repo", "jasperhg90/skills"])
    assert res.exit_code != 0
    assert "not a registered repo alias" in _plain(res.output)


def test_enrich_from_index_fills_repos_and_shas(home: Path, tmp_path: Path) -> None:
    working = git_fixtures.make_source_repo(
        tmp_path / "src",
        files={"skills/foo/SKILL.md": "# foo\n", "rules/bar.md": "# bar\n"},
    )
    bare = git_fixtures.make_bare_remote(working, tmp_path / "bare.git")
    repos.add("src", f"file://{bare}")
    head = subprocess.run(
        ["git", "-C", str(working), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # A builder-style template: artifacts by qualified_name only, no repos/SHAs.
    built = profiles.Profile(
        name="python-base",
        skills=[profiles.ProfileSkill(qualified_name="src/foo")],
        rules=[profiles.ProfileRule(qualified_name="src/bar")],
    )

    enriched = profiles.enrich_from_index(built)

    assert enriched.repos == [profiles.ProfileRepo(alias="src", url=f"file://{bare}")]
    assert enriched.skills[0].sha == head
    assert enriched.rules[0].sha == head


def test_export_unresolved_repo_is_friendly(home: Path, tmp_path: Path) -> None:
    # A saved template referencing an unregistered repo must export with a clean
    # `error:` message, never a raw traceback (the export now always enriches).
    profiles.save(
        profiles.Profile(name="orphan", skills=[profiles.ProfileSkill(qualified_name="ghost/foo")])
    )
    res = _runner.invoke(cli.app, ["template", "export", "orphan", str(tmp_path / "o.toml")])
    assert res.exit_code == 1
    assert "error:" in _plain(res.output)
    assert "Traceback" not in res.output


def test_enrich_from_index_unresolved_artifact_raises(home: Path) -> None:
    built = profiles.Profile(
        name="broken",
        skills=[profiles.ProfileSkill(qualified_name="ghost/foo")],
    )
    with pytest.raises(profiles.TemplateArtifactUnresolvedError):
        profiles.enrich_from_index(built)
