from __future__ import annotations

from pathlib import Path

import pytest

from aim.core import content_guard


def test_scan_text_finds_zero_width_characters() -> None:
    text = "safe​text"
    findings = content_guard.scan_text(text)
    assert len(findings) == 1
    assert "U+200B" in findings[0]


def test_scan_text_finds_bom() -> None:
    text = "﻿hello"
    findings = content_guard.scan_text(text)
    assert len(findings) == 1
    assert "U+FEFF" in findings[0]


def test_scan_text_finds_bidi_controls() -> None:
    text = "a‮bc‬d"
    findings = content_guard.scan_text(text)
    assert len(findings) == 2
    assert any("U+202E" in f for f in findings)
    assert any("U+202C" in f for f in findings)


def test_scan_text_finds_tag_characters() -> None:
    text = "x\U000e0001y"
    findings = content_guard.scan_text(text)
    assert len(findings) == 1
    assert "U+E0001" in findings[0]


def test_scan_text_reports_line_and_column() -> None:
    text = "line1\nline2​"
    findings = content_guard.scan_text(text)
    assert len(findings) == 1
    assert "line 2" in findings[0]
    assert "column 6" in findings[0]


def test_scan_text_safe_ascii_is_empty() -> None:
    assert content_guard.scan_text("plain ASCII text") == []


def test_scan_text_safe_unicode_is_empty() -> None:
    assert content_guard.scan_text("日本語 émoji") == []


def test_assert_no_hidden_unicode_raises() -> None:
    with pytest.raises(content_guard.HiddenUnicodeError) as exc:
        content_guard.assert_no_hidden_unicode("ok​", source="skill.md")
    assert "skill.md" in str(exc.value)


def test_assert_no_hidden_unicode_passes_for_safe_text() -> None:
    content_guard.assert_no_hidden_unicode("safe text")


def test_scan_file_finds_hidden_character(tmp_path: Path) -> None:
    path = tmp_path / "skill.md"
    path.write_text("# Skill\n​", encoding="utf-8")
    findings = content_guard.scan_file(path)
    assert len(findings) == 1


def test_scan_directory_finds_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("ok", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("bad‎", encoding="utf-8")
    findings = content_guard.scan_directory(tmp_path)
    assert len(findings) == 1
    assert "b.md" in findings[0]


def test_scan_directory_skips_non_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "binary").write_bytes(b"\xff\xfe")
    assert content_guard.scan_directory(tmp_path) == []


def test_require_secure_url_blocks_http() -> None:
    with pytest.raises(content_guard.InsecureTransportError):
        content_guard.require_secure_url("http://example.com/repo.git")


def test_require_secure_url_allows_https() -> None:
    content_guard.require_secure_url("https://example.com/repo.git")


def test_require_secure_url_allows_file_and_ssh() -> None:
    content_guard.require_secure_url("file:///tmp/repo.git")
    content_guard.require_secure_url("git@github.com:foo/bar.git")
    content_guard.require_secure_url("ssh://git@github.com/foo/bar.git")


def test_require_secure_url_allows_http_with_flag() -> None:
    content_guard.require_secure_url("http://example.com/repo.git", allow_insecure=True)
