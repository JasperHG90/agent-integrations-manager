from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aim import cli
from aim.core import user_config

_runner = CliRunner()


def test_defaults_when_file_missing(home: Path) -> None:
    cfg = user_config.load()
    assert cfg.tui.filters.show_plugin_owned is True
    assert cfg.tui.filters.show_dot_claude is True
    assert not user_config.config_path().exists()


def test_save_load_roundtrip(home: Path) -> None:
    cfg = user_config.load()
    cfg.tui.filters.show_plugin_owned = True
    user_config.save(cfg)
    assert user_config.config_path().exists()
    assert user_config.load().tui.filters.show_plugin_owned is True


def test_set_get_unset_dotted_keys(home: Path) -> None:
    user_config.set_value("tui.filters.show_plugin_owned", "true")
    assert user_config.get("tui.filters.show_plugin_owned") is True
    user_config.set_value("tui.filters.show_dot_claude", "off")
    assert user_config.get("tui.filters.show_dot_claude") is False
    user_config.unset("tui.filters.show_plugin_owned")
    assert user_config.get("tui.filters.show_plugin_owned") is True  # default


def test_unknown_key_raises(home: Path) -> None:
    with pytest.raises(user_config.UnknownConfigKeyError):
        user_config.get("tui.filters.nope")
    with pytest.raises(user_config.UnknownConfigKeyError):
        user_config.set_value("nope.deep.key", "1")
    with pytest.raises(user_config.UnknownConfigKeyError):
        user_config.get("tui")  # nested model, not a leaf


def test_invalid_bool_raises(home: Path) -> None:
    with pytest.raises(user_config.InvalidConfigValueError):
        user_config.set_value("tui.filters.show_plugin_owned", "maybe")


def test_corrupt_file_yields_defaults(home: Path) -> None:
    path = user_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is [not toml")
    cfg = user_config.load()
    assert cfg.tui.filters.show_plugin_owned is True


def test_unknown_keys_on_disk_ignored(home: Path) -> None:
    path = user_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('version = 1\nfuture_key = "x"\n\n[tui.filters]\nshow_plugin_owned = true\n')
    assert user_config.load().tui.filters.show_plugin_owned is True


def test_list_values_reports_leaves_with_defaults(home: Path) -> None:
    user_config.set_value("tui.filters.show_plugin_owned", "true")
    rows = {key: (value, default) for key, value, default in user_config.list_values()}
    assert rows["tui.filters.show_plugin_owned"] == (True, True)
    assert rows["tui.filters.show_dot_claude"] == (True, True)


# ---------------------------------------------------------------------------
# `aim app config` CLI
# ---------------------------------------------------------------------------


def test_cli_config_set_get_list_path(home: Path) -> None:
    res = _runner.invoke(cli.app, ["app", "config", "set", "tui.filters.show_plugin_owned", "true"])
    assert res.exit_code == 0, res.output
    assert "set tui.filters.show_plugin_owned = True" in res.output

    res = _runner.invoke(cli.app, ["app", "config", "get", "tui.filters.show_plugin_owned"])
    assert res.exit_code == 0, res.output
    assert "True" in res.output

    res = _runner.invoke(cli.app, ["app", "config", "list"])
    assert res.exit_code == 0, res.output
    assert "tui.filters.show_plugin_owned" in res.output

    res = _runner.invoke(cli.app, ["app", "config", "path"])
    assert res.exit_code == 0, res.output
    assert "config.toml" in res.output

    res = _runner.invoke(cli.app, ["app", "config", "unset", "tui.filters.show_plugin_owned"])
    assert res.exit_code == 0, res.output
    assert user_config.get("tui.filters.show_plugin_owned") is True  # default


def test_cli_config_unknown_key_errors(home: Path) -> None:
    res = _runner.invoke(cli.app, ["app", "config", "get", "no.such.key"])
    assert res.exit_code != 0
    res = _runner.invoke(cli.app, ["app", "config", "set", "no.such.key", "1"])
    assert res.exit_code != 0


def test_cli_config_bad_bool_errors(home: Path) -> None:
    res = _runner.invoke(
        cli.app, ["app", "config", "set", "tui.filters.show_plugin_owned", "maybe"]
    )
    assert res.exit_code != 0


def test_version_is_not_user_addressable(home: Path) -> None:
    with pytest.raises(user_config.UnknownConfigKeyError):
        user_config.set_value("version", "5")
    with pytest.raises(user_config.UnknownConfigKeyError):
        user_config.get("version")
    assert all(key != "version" for key, _, _ in user_config.list_values())
