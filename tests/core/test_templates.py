from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import content_guard, templates
from aim.core.models import RenderRule


def test_resolve_builtin_default(home: Path) -> None:
    t = templates.resolve(templates.BUILTIN_DEFAULT)
    assert t.name == templates.BUILTIN_DEFAULT
    assert "aim" in t.body


def test_resolve_unknown_raises(home: Path) -> None:
    with pytest.raises(templates.TemplateNotFoundError):
        templates.resolve("does-not-exist")


def test_list_includes_builtin(home: Path) -> None:
    names = [t.name for t in templates.list_templates()]
    assert templates.BUILTIN_DEFAULT in names


def test_render_with_rules(home: Path) -> None:
    rules = [
        RenderRule(name="be-concise", body="Be concise.", description="brevity"),
    ]
    out = templates.render(
        templates.BUILTIN_DEFAULT,
        {"rules": rules, "rules_mode": "inline"},
    )
    assert "Be concise." in out
    assert "be-concise" in out


def test_render_inline_with_no_rules_emits_empty_block(home: Path) -> None:
    out = templates.render(templates.BUILTIN_DEFAULT, {"rules": [], "rules_mode": "inline"})
    assert "<!-- BEGIN aim: rules -->" in out
    assert "<!-- END aim: rules -->" in out
    assert "### " not in out


def test_render_files_mode_omits_rules_block(home: Path) -> None:
    out = templates.render(templates.BUILTIN_DEFAULT, {"rules": [], "rules_mode": "files"})
    assert "aim: rules" not in out
    assert "## Applied rules" not in out


def test_render_guidelines_region_present_in_both_modes(home: Path) -> None:
    for mode in ("files", "inline"):
        out = templates.render(templates.BUILTIN_DEFAULT, {"rules": [], "rules_mode": mode})
        assert "<!-- BEGIN aim: guidelines -->" in out
        assert "<!-- END aim: guidelines -->" in out
        assert "Think Before Coding" in out


def test_register_user_template(home: Path, tmp_path: Path) -> None:
    custom = tmp_path / "custom.md.j2"
    custom.write_text("# Custom\n\n<!-- BEGIN aim: header -->\nhi\n<!-- END aim: header -->\n")
    templates.register_user_template("custom", custom, description="my template")
    resolved = templates.resolve("custom")
    assert resolved.name == "custom"
    assert "Custom" in resolved.body


def test_register_user_template_missing_file_raises(home: Path, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        templates.register_user_template("ghost", tmp_path / "nope.md.j2")


def test_resolve_rejects_hidden_unicode_user_template(home: Path, tmp_path: Path) -> None:
    custom = tmp_path / "custom.md.j2"
    custom.write_text("# Custom\n\nhidden​\n")
    templates.register_user_template("custom", custom, description="my template")
    with pytest.raises(content_guard.HiddenUnicodeError):
        templates.resolve("custom")
