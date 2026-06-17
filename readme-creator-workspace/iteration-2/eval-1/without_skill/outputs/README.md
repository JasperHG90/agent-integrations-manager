<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="480">
</p>

<h1 align="center">agent-init</h1>

<p align="center">
  Scaffold agent-engineering projects, manage reusable rules, and install Claude skills, agents, and MCP servers from git repos.
</p>

<p align="center">
  <img src="assets/demo.gif" alt="agent-init TUI demo" width="640">
</p>

## Status

v0.1.0 — macOS / Linux. Windows is not supported in this release.

## Install

Run with `uvx` directly from GitHub:

```sh
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --version
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init tui
```

Install permanently as a `uv` tool:

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
# 1. Add a reusable rule and mark it as a default for every new project.
agent-init rule add be-concise --body "Be concise." --default

# 2. Initialize a project: writes AGENTS.md + CLAUDE.md/GEMINI.md mirrors and seeds default rules.
agent-init init path/to/project --mirror CLAUDE.md --mirror GEMINI.md

# 3. Register a skill source repo (any git URL: https, ssh, or file://).
agent-init repo add anthropic https://github.com/anthropics/skills

# 4. Browse, search, and install skills.
agent-init skill list
agent-init skill search review
agent-init skill install anthropic/code-review

# 5. Later: refresh upstream and update or roll back.
agent-init repo refresh anthropic
agent-init skill update anthropic/code-review
agent-init skill rollback anthropic/code-review

# Or use the interactive TUI.
agent-init tui
```

## Features

- **Project scaffolding** — generates `AGENTS.md` from managed templates, with optional mirror files (`CLAUDE.md`, `GEMINI.md`, etc.) and symlinks.
- **Reusable rules** — global rule library; mark rules as default so they auto-seed into every new project.
- **Skill management** — register git repos, index skills, install/update/rollback with per-skill version pinning.
- **Sub-agent management** — install and version Claude Code sub-agents from git repos the same way as skills.
- **MCP servers** — search the public MCP registry and install servers into `.mcp.json` with version pinning.
- **Project profiles** — snapshot a project configuration (skills, agents, MCP servers, rules) and re-apply it elsewhere.
- **Drift detection** — `check` for pre-commit-friendly drift checks; `doctor` for a full audit across configured project roots.
- **Interactive TUI** — browse repos, skills, agents, rules, and profiles in a Textual interface.

## Command overview

| Command | Purpose |
| --- | --- |
| `agent-init init <project>` | Scaffold or refresh `AGENTS.md`, mirrors, and rules |
| `agent-init rule add/list/edit/delete` | Manage the global rule library |
| `agent-init repo add/list/refresh/remove/rename` | Register skill/agent source repos |
| `agent-init skill list/search/install/update/delete/rollback` | Manage installed skills |
| `agent-init agent list/search/install/update/delete/rollback` | Manage installed sub-agents |
| `agent-init mcp search/list/install/update/delete` | Manage MCP servers via the registry |
| `agent-init profile save/list/show/apply/delete` | Save and apply project templates |
| `agent-init check` | Pre-commit drift check |
| `agent-init doctor` | Full audit across configured roots |
| `agent-init tui` | Launch the interactive TUI |

## How it works

Per-project state lives in `.agent-init/manifest.json` (committed to the repo). It pins installed skills, agents, and MCP servers to exact versions and keeps a rollback history.

Global machine-local state lives under [platformdirs](https://platformdirs.readthedocs.io/):

- `user_data_dir` — SQLite cache of registered repos, indexed skills/agents, templates, and rule metadata.
- `user_cache_dir/repos/<alias>` — bare git mirrors reused across projects.
- `user_cache_dir/snapshots/<alias>/<sha>/<skill>` — extracted skill bytes for rollback when upstream SHAs are no longer reachable.
- `user_config_dir/rules` — user-authored rule snippets.

The global SQLite DB is a **cache**. The project's `manifest.json` is the **source of truth** for what is installed.

## Skill and agent discovery

A registered repo must expose skills or agents at one of these paths (precedence high → low):

1. `skills/<name>/SKILL.md` or `agents/<name>.md`
2. `.claude/skills/<name>/SKILL.md` or `.claude/agents/<name>.md`
3. `<name>/SKILL.md` or `<name>.md` at repo root
4. `SKILL.md` at repo root (the repo alias becomes the skill name)

Skills are referenced as `<repo_alias>/<skill_name>`; agents as `<repo_alias>/<agent_name>`. Repos with no discoverable artifacts are rejected on `repo add` unless `--allow-empty` is passed.

## Safety properties

- `repo add` rolls back cleanly on indexing failure.
- `git archive | tar` extraction surfaces git's stderr first.
- Snapshots write a completion sentinel; partial extractions are re-run on next access.
- `skill update` refuses to overwrite hand-edits unless `--force` is used.
- `init` warns when in-region content was edited by hand since the last write.
- `repo rename` rewrites the registry and skill index atomically.
- Rollback prefers local snapshots and errors loudly if neither snapshot nor upstream is available.

## Development

```sh
uv run pytest          # full suite — 100+ tests, including TUI Pilot + snapshot tests
uv run ruff check .    # lint
uv run agent-init tui  # launch the TUI

pytest tests/tui --snapshot-update  # only after intentional visual changes
```
