from __future__ import annotations

import pytest

from aim.cli import _looks_like_url, _parse_source_url
from aim.cli._shared import _parse_source_subpath


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # GitHub tree URL pointing at a skill directory.
        (
            "https://github.com/netresearch/skill-repo-skill/tree/main/skills/skill-repo",
            ("https://github.com/netresearch/skill-repo-skill", "main", "skill-repo"),
        ),
        # GitHub blob URL pointing at a SKILL.md → name is the parent directory.
        (
            "https://github.com/org/repo/blob/main/skills/foo/SKILL.md",
            ("https://github.com/org/repo", "main", "foo"),
        ),
        # Rule file → name is the file stem.
        (
            "https://github.com/org/repo/blob/main/rules/be-concise.md",
            ("https://github.com/org/repo", "main", "be-concise"),
        ),
        # GitLab uses a `-/tree` segment.
        (
            "https://gitlab.com/org/repo/-/tree/develop/agents/python-pro",
            ("https://gitlab.com/org/repo", "develop", "python-pro"),
        ),
        # Plain clone URLs pass through unchanged with no ref/name.
        ("https://github.com/org/repo", ("https://github.com/org/repo", None, None)),
        ("https://github.com/org/repo.git", ("https://github.com/org/repo.git", None, None)),
        ("git@github.com:org/repo.git", ("git@github.com:org/repo.git", None, None)),
    ],
)
def test_parse_source_url(url: str, expected: tuple[str, str | None, str | None]) -> None:
    assert _parse_source_url(url) == expected


@pytest.mark.parametrize(
    ("target", "is_url"),
    [
        ("https://github.com/org/repo", True),
        ("http://example.com/a/b", True),
        ("git@github.com:org/repo.git", True),
        ("alias/name", False),
        ("remove", False),
        ("just-a-word", False),
    ],
)
def test_looks_like_url(target: str, is_url: bool) -> None:
    assert _looks_like_url(target) is is_url


@pytest.mark.parametrize(
    ("url", "subpath"),
    [
        # Blob URL to an instruction file keeps the full in-repo path.
        ("https://github.com/org/repo/blob/main/docs/AGENTS.md", "docs/AGENTS.md"),
        ("https://github.com/org/repo/blob/main/AGENTS.md", "AGENTS.md"),
        # Tree URL to a directory.
        ("https://gitlab.com/org/repo/-/tree/develop/instructions/lean", "instructions/lean"),
        # Pasted-link noise: GitHub's raw/plain toggle and copy-permalink line
        # anchors are not part of the in-repo path.
        ("https://github.com/org/repo/blob/main/AGENTS.md?plain=1", "AGENTS.md"),
        ("https://github.com/org/repo/blob/main/docs/AGENTS.md#L1-L20", "docs/AGENTS.md"),
        ("https://github.com/org/repo/blob/main/docs/AGENTS.md?plain=1#L10", "docs/AGENTS.md"),
        # Plain clone URLs carry no subpath.
        ("https://github.com/org/repo", None),
        ("git@github.com:org/repo.git", None),
    ],
)
def test_parse_source_subpath(url: str, subpath: str | None) -> None:
    assert _parse_source_subpath(url) == subpath


def test_parse_source_url_strips_query_and_fragment() -> None:
    assert _parse_source_url("https://github.com/org/repo/blob/main/AGENTS.md?plain=1") == (
        "https://github.com/org/repo",
        "main",
        "AGENTS",
    )
