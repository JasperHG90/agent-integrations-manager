from __future__ import annotations

import pytest

from atm.core import agents_md


def test_parse_returns_regions_in_order() -> None:
    text = """# Title

<!-- BEGIN atm: a -->
A body
<!-- END atm: a -->

middle

<!-- BEGIN atm: b -->
B body
<!-- END atm: b -->
"""
    regions = agents_md.parse(text)
    assert [r.name for r in regions] == ["a", "b"]
    assert "A body" in regions[0].body
    assert "B body" in regions[1].body


def test_parse_empty_when_no_markers() -> None:
    assert agents_md.parse("Hello world.\n") == []


def test_parse_raises_on_unbalanced_markers() -> None:
    text = "<!-- BEGIN atm: a -->\nbody\n"
    with pytest.raises(agents_md.RegionError):
        agents_md.parse(text)


def test_parse_legacy_agent_init_markers_raise_clear_error() -> None:
    text = "<!-- BEGIN agent-init: header -->\nold content\n<!-- END agent-init: header -->\n"
    with pytest.raises(agents_md.LegacyMarkerError) as exc_info:
        agents_md.parse(text)
    assert "legacy agent-init markers" in str(exc_info.value)
    assert "migrate to atm markers" in str(exc_info.value)


def test_merge_replaces_existing_region_preserving_outside() -> None:
    existing = """User preamble.

<!-- BEGIN atm: rules -->
old rule body
<!-- END atm: rules -->

User postlude.
"""
    out = agents_md.merge(existing, {"rules": "new rule body"})
    assert "User preamble." in out
    assert "User postlude." in out
    assert "old rule body" not in out
    assert "new rule body" in out


def test_merge_appends_missing_region() -> None:
    existing = "Hello.\n"
    out = agents_md.merge(existing, {"new": "fresh content"})
    assert "Hello." in out
    assert "<!-- BEGIN atm: new -->" in out
    assert "fresh content" in out


def test_merge_leaves_unmentioned_regions_alone() -> None:
    existing = """<!-- BEGIN atm: a -->
A
<!-- END atm: a -->
<!-- BEGIN atm: b -->
B
<!-- END atm: b -->
"""
    out = agents_md.merge(existing, {"a": "A2"})
    assert "A2" in out
    assert "B" in out  # untouched
    assert out.count("BEGIN atm: b") == 1


def test_build_from_scratch() -> None:
    out = agents_md.build([("a", "alpha"), ("b", "beta")])
    assert "BEGIN atm: a" in out
    assert "alpha" in out
    assert "BEGIN atm: b" in out
    assert "beta" in out


def test_merge_idempotent_when_body_unchanged() -> None:
    existing = """<!-- BEGIN atm: r -->
same body
<!-- END atm: r -->
"""
    once = agents_md.merge(existing, {"r": "same body"})
    twice = agents_md.merge(once, {"r": "same body"})
    assert once == twice
