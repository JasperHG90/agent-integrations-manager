"""CLI coverage for `aim template` (and the back-compat `aim profile` alias)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

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
    profiles.save(
        profiles.Profile(
            name="svc",
            description="service template",
            repos=[profiles.ProfileRepo(alias="acme", url="https://example.com/acme.git")],
            skills=[profiles.ProfileSkill(qualified_name="acme/foo")],
        )
    )
    out = tmp_path / "svc.toml"

    res = _runner.invoke(cli.app, ["template", "export", "svc", str(out)])
    assert res.exit_code == 0, _plain(res.output)
    assert out.exists()

    res = _runner.invoke(cli.app, ["template", "import", str(out), "--name", "svc2"])
    assert res.exit_code == 0, _plain(res.output)

    imported = profiles.load("svc2")
    assert imported.name == "svc2"
    assert imported.description == "service template"
    assert imported.repos == [
        profiles.ProfileRepo(alias="acme", url="https://example.com/acme.git")
    ]
    assert [s.qualified_name for s in imported.skills] == ["acme/foo"]


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
