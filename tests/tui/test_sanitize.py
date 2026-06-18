from __future__ import annotations

from aim.tui.modals.repo_add import sanitize_repo_alias


def test_repo_alias_sanitization() -> None:
    assert sanitize_repo_alias("Anthropic Skills") == "anthropic-skills"
    assert sanitize_repo_alias("My/Org-Repo") == "my-org-repo"
    assert sanitize_repo_alias("--leading") == "leading"
