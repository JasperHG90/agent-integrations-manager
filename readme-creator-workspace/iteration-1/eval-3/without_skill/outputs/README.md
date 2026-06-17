<div align="center">

<img src="assets/logo.png" alt="agent-init logo" width="420">

# agent-init

A lightweight CLI and TUI for scaffolding agent-engineering projects.

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/jasperginn/agent-init)
[![Python](https://img.shields.io/badge/python-3.10%2B-306998.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-100%2B-brightgreen.svg)](./tests)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

</div>

---

## What it does

`agent-init` gives you one repeatable workflow for bootstrapping projects that work well with coding agents:

- Generate `AGENTS.md` from a managed template, with mirrored variants such as `CLAUDE.md` and `GEMINI.md`.
- Maintain a reusable library of rule snippets, and mark any rule as a default so it seeds into every new project.
- Register skill source repositories globally, then install, update, and roll back skills with per-skill version pinning.

<div align="center">
  <img src="assets/demo.gif" alt="agent-init TUI demo" width="640">
</div>

---

## Quick start

Install and run with `uvx`:

```sh
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --version
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init tui
```

Or install permanently as a `uv` tool:

```sh
uv tool install git+https://github.com/jasperginn/agent-init.git
```

For local development:

```sh
git clone https://github.com/jasperginn/agent-init.git
cd agent_init
uv sync
uv run agent-init --version
```

---

## Usage

```sh
# 1. Add a rule snippet and mark it as a default.
agent-init rule add be-concise --body "Be concise." --default

# 2. Initialize a project: writes AGENTS.md + CLAUDE.md/GEMINI.md mirrors, seeds the default rule.
agent-init init path/to/project

# 3. Register a skill source repo (https, ssh, or file://).
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

---

## Architecture

### Per-project state

Each project keeps its state in `.agent-init/manifest.json`, committed alongside your code. The manifest pins installed skills to a `(tag, sha)` pair and stores the last 10 versions in `history`, so rollback works even on a fresh clone.

### Global state

Global, machine-local state lives under [platformdirs](https://platformdirs.readthedocs.io/):

- `user_data_dir`: SQLite cache of registered repos, indexed skills, templates, and rule metadata.
- `user_cache_dir/repos/<alias>`: bare git mirrors (`git clone --mirror`), reused across projects.
- `user_cache_dir/snapshots/<alias>/<sha>/<skill>`: extracted skill bytes, used by rollback when the upstream SHA is no longer reachable.
- `user_config_dir/rules`: user-authored rule snippets, one Markdown file per rule.

The global SQLite DB is treated as a cache. The project's `manifest.json` is the source of truth for what is installed where.

### Skill discovery convention

A registered repo must expose at least one skill at one of these paths, in order of precedence:

1. `skills/<name>/SKILL.md`
2. `.claude/skills/<name>/SKILL.md`
3. `<name>/SKILL.md` at repo root
4. `SKILL.md` at repo root (the repo alias becomes the skill name)

Skills are referenced everywhere as `<repo_alias>/<skill_name>`. Repos with no discoverable skills are rejected on `repo add` unless `--allow-empty` is passed.

### Versioning

Skill versions are pinned as `<tag>+<short_sha>` when a tag both (a) contains the skill at that revision and (b) is at or after the skill's last-touching commit; otherwise the pin is SHA-only. On `update`, the resolver only attaches the tag when the install honestly reflects it.

---

## Safety properties

- `repo add` rolls back cleanly on indexing failure: no orphan registrations.
- `git archive | tar` extraction surfaces git's stderr first; `tar` errors are never misattributed.
- Snapshots write a `.agent-init.complete` sentinel; partial extractions are re-run on next access.
- `skill update` refuses to overwrite hand-edits to the deployed target directory by comparing `content_hash`; use `--force` to override.
- `init` warns when it overwrites in-region content that was edited by hand since the last write.
- `repo rename` rewrites the SQLite registry and skill index atomically; if the on-disk clone move fails, the DB rename is rolled back.
- Rollback prefers the local snapshot; if both snapshot and upstream are gone, it errors out loudly rather than silently no-op'ing.

---

## Status

Version 0.1 — macOS and Linux only. Windows support is not included in this release.

---

## Development

```sh
uv run pytest          # full suite, including TUI Pilot and snapshot tests
uv run ruff check .    # lint
uv run agent-init tui  # launch the TUI

pytest tests/tui --snapshot-update  # only after intentional visual changes
```
