"""Spec-time validation of declarative plugin kinds (KindSpec)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aim.core.plugin_kinds import KindSpec


def _spec(*, vendor_into: str = ".opencode/plugins/{name}", config_file: str | None = None) -> dict:
    register: dict = {"vendor_into": vendor_into}
    if config_file is not None:
        register["config"] = [{"file": config_file}]
    return {"name": "opencode", "manifest": {"file": "package.json"}, "register": register}


def test_valid_relative_paths_pass() -> None:
    KindSpec.model_validate(
        _spec(vendor_into=".opencode/plugins/{name}", config_file=".x/config.json")
    )


@pytest.mark.parametrize(
    "bad",
    ["/abs/{name}", "../escape/{name}", ".ok/../../etc/{name}", "\\\\abs\\{name}"],
)
def test_absolute_or_parent_escaping_vendor_into_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        KindSpec.model_validate(_spec(vendor_into=bad))


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../x.json", ".a/../../b.json"])
def test_absolute_or_parent_escaping_config_file_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        KindSpec.model_validate(_spec(config_file=bad))
