<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="480">
</p>

`agent-init` scaffolds agent-engineering projects, manages reusable rules, and installs Claude skills from git repos. It gives every project a consistent `AGENTS.md` baseline, a shareable rule library, and version-pinned skills that can be updated or rolled back.

<p align="center">
  <img src="assets/demo.gif" alt="agent-init TUI demo" width="640">
</p>

## Status

v0.1 — macOS / Linux only. Windows is not supported in this release.

## Install

The recommended way to run `agent-init` is via `uvx` from this repo:

```sh
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --version
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init tui
```

To install permanently as a `uv` tool:

```sh
uv tool install git+https://github.com/jasperginn/agent-init.git
```

For local development:

```sh
git clone https://github.com/jasperginn/agent-init.git
cd agent-init
uv sync
uv run agent-init --version
```

## Quick start

```sh
# 1. Add a reusable rule and make it seed into every new project.
agent-init rule add be-concise --body "Be concise." --default

# 2. Initialize a project: writes AGENTS.md plus mirrors like CLAUDE.md / GEMINI.md.
agent-init init path/to/project

# 3. Register a skill source repo (https, ssh, or file:// all work).
agent-init repo add anthropic https://github.com/anthropics/skills

# 4. Browse, search, and install skills.
agent-init skill list
agent-init skill search review
agent-init skill install anthropic/code-review

# 5. Keep skills current, or roll back when something breaks.
agent-init repo refresh anthropic
agent-init skill update anthropic/code-review
agent-init skill rollback anthropic/code-review

# Or use the TUI.
agent-init tui
```

## How it works

Project state is stored in `.agent-init/manifest.json` (committed with your repo). It pins every installed skill to a `(tag, sha)` pair and keeps the last 10 versions in `history`, so rollback works even on a fresh clone.

Global, machine-local state is kept under [platformdirs](https://platformdirs.readthedocs.io/):

- `user_data_dir`: SQLite cache of registered repos, indexed skills, templates, and rule metadata.
- `user_cache_dir/repos/<alias>`: bare git mirrors, reused across projects.
- `user_cache_dir/snapshots/<alias>/<sha>/<skill>`: extracted skill bytes used by rollback when upstream no longer has the SHA.
- `user_config_dir/rules`: user-authored rule snippets, one markdown file per rule.

The SQLite database is a **cache**; the project's `manifest.json` is the **source of truth** for what is installed.

## Skill discovery convention

A registered repo must expose at least one skill at one of these paths, in this order:

1. `skills/<name>/SKILL.md`
2. `.claude/skills/<name>/SKILL.md`
3. `<name>/SKILL.md` at repo root
4. `SKILL.md` at repo root (the repo alias becomes the skill name)

Skills are referenced everywhere as `<repo_alias>/<skill_name>`. Repos with no discoverable skills are rejected on `repo add` unless you pass `--allow-empty`.

## Versioning

Skill versions are pinned as `<tag>+<short_sha>` when a tag both contains the skill at that revision and is at or after the skill's last-touching commit; otherwise they are SHA-only. On `update`, the resolver only attaches the tag when the install honestly reflects it.

## Safety properties

- `repo add` rolls back cleanly on indexing failure, leaving no orphan registrations.
- `git archive | tar` extraction surfaces git's stderr first so `tar` errors are never misattributed.
- Snapshots write a `.agent-init.complete` sentinel; partial extractions are re-run on next access.
- `skill update` refuses to overwrite hand-edits in the deployed target directory (checked via `content_hash`); use `--force` to override.
- `init` warns when it overwrites in-region content that was edited by hand since the last write.
- `repo rename` rewrites the SQLite registry and skill index atomically; if the on-disk clone move fails, the DB rename is rolled back.
- Rollback prefers the local snapshot; if both snapshot and upstream are gone, it fails loudly instead of silently no-op'ing.

## Development

```sh
uv run pytest          # full suite — 100+ tests, including TUI Pilot + snapshot tests
uv run ruff check .    # lint
uv run agent-init tui  # launch the TUI

pytest tests/tui --snapshot-update  # only after intentional visual changes
```
