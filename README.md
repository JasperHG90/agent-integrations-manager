<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="480">
</p>

A small CLI + TUI for scaffolding agent-engineering projects.

- Generates `AGENTS.md` from a managed template, with mirrors (`CLAUDE.md`, `GEMINI.md`, ...).
- Manages a library of reusable rule snippets, with a "default" flag so chosen rules auto-seed into every new project.
- Registers skill source repositories globally; installs, updates, and rolls back skills into projects with per-skill version pinning.

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
git clone <this repo>
cd agent_init
uv sync
uv run agent-init --version
```

## Quick start

```sh
# 1. Add a rule snippet and mark it as a default.
agent-init rule add be-concise --body "Be concise." --default

# 2. Initialize a project: writes AGENTS.md + CLAUDE.md/GEMINI.md mirrors, seeds the default rule.
agent-init init path/to/project

# 3. Register a skill source repo (any git URL — https, ssh, or file://).
agent-init repo add anthropic https://github.com/anthropics/skills

# 4. Browse skills, search by substring, install into the current project.
agent-init skill list
agent-init skill search review
agent-init skill install anthropic/code-review

# 5. Later: refresh upstream, update or rollback.
agent-init repo refresh anthropic
agent-init skill update anthropic/code-review
agent-init skill rollback anthropic/code-review

# Or use the TUI.
agent-init tui
```

## How it works

Per-project state lives at `.agent-init/manifest.json` (committed to your repo). It pins installed skills to a `(tag, sha)` pair and keeps the last 10 versions in `history` so rollback works on a fresh clone too.

Global, machine-local state lives under [platformdirs](https://platformdirs.readthedocs.io/):

- `user_data_dir`: SQLite cache of registered repos, indexed skills, templates, and rule metadata.
- `user_cache_dir/repos/<alias>`: bare git mirrors (`git clone --mirror`), reused across projects.
- `user_cache_dir/snapshots/<alias>/<sha>/<skill>`: extracted skill bytes, used by rollback when the upstream SHA is no longer reachable.
- `user_config_dir/rules`: user-authored rule snippets (one markdown file per rule).

The global SQLite DB is treated as a **cache**. The project's `manifest.json` is the **source of truth** for what's installed where.

## Skill discovery convention

A registered repo must expose at least one skill at one of these paths (precedence high → low):

1. `skills/<name>/SKILL.md`
2. `.claude/skills/<name>/SKILL.md`
3. `<name>/SKILL.md` at repo root
4. `SKILL.md` at repo root (the repo alias becomes the skill name)

Skills are referenced everywhere as `<repo_alias>/<skill_name>`. Repos with no discoverable skills are rejected on `repo add` (pass `--allow-empty` to override).

## Versioning

Skill versions are pinned as `<tag>+<short_sha>` when a tag both (a) contains the skill at that revision and (b) is at or after the skill's last-touching commit; otherwise SHA-only. On `update`, the resolver only attaches the tag when the install honestly reflects it.

## Safety properties

- `repo add` rolls back cleanly on indexing failure: no orphan registrations.
- `git archive | tar` extraction surfaces git's stderr first; `tar` errors are never misattributed.
- Snapshots write a `.agent-init.complete` sentinel; partial extractions are re-run on next access.
- `skill update` refuses to overwrite hand-edits to the deployed target directory (compare `content_hash`); use `--force` to override.
- `init` warns when it overwrites in-region content that was edited by hand since the last write.
- `repo rename` rewrites the SQLite registry and skill index atomically; if the on-disk clone move fails, the DB rename is rolled back.
- Rollback prefers the local snapshot; if both snapshot and upstream are gone, it errors out loudly rather than silently no-op'ing.

## Dev

```sh
uv run pytest          # full suite — currently 100+ tests, including TUI Pilot + snapshot tests
uv run ruff check .    # lint
uv run agent-init tui  # launch the TUI

pytest tests/tui --snapshot-update  # only after intentional visual changes
```
