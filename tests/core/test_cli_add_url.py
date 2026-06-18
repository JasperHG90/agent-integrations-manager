from __future__ import annotations

import pytest

from aim.cli import _parse_source_url


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
