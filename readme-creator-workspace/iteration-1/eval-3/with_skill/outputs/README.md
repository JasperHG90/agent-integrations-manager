<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="220">
</p>

<h1 align="center">agent-init</h1>

<p align="center">
  <strong>Scaffold agent-engineering projects with versioned skills, reusable rules, and managed AGENTS.md mirrors.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-6366f1" alt="version 0.1.0">
  <img src="https://img.shields.io/badge/python-3.12%2B-0ea5e9" alt="python 3.12+">
  <img src="https://img.shields.io/badge/platform-macos%20%7C%20linux-64748b" alt="platform macos linux">
  <img src="https://img.shields.io/badge/tests-100%2B-22c55e" alt="tests 100+">
</p>

<p align="center">
  <img src="assets/demo.gif" alt="agent-init TUI demo showing repo, skill, rule, and project flows" width="720">
</p>

## Why agent-init exists

Agent-aware projects need more than code. They need clear instructions for the assistant: how to write, what rules to follow, and which reusable skills to install. Without a system, every team reinvents a brittle pile of markdown files and copy-pasted snippets.

`agent-init` turns that chaos into a reproducible workflow. It generates `AGENTS.md` from a managed template, keeps mirror files like `CLAUDE.md` and `GEMINI.md` in sync, and installs version-pinned skills from any git repository. Your AI assistant gets the right context from day one — and keeps it.

## Features

- **One command to scaffold agent context.** `agent-init init` writes `AGENTS.md` and its mirrors, then seeds your default rules into the project.
- **Reusable rule library.** Add rule snippets once, mark the ones you always want, and let them auto-attach to every new project.
- **Skills from anywhere.** Register git repos as skill sources — https, ssh, or `file://` — then search, install, update, and roll back with one command.
- **Version pinning that survives clones.** Installed skills are pinned to a `(tag, sha)` pair and the last 10 versions are kept in `history`.
- **Safe by default.** Rollback refuses silent no-ops, updates refuse to overwrite hand-edits without `--force`, and `repo add` rolls back cleanly on indexing failure.
- **Interactive TUI.** Browse repos, skills, rules, and projects with a keyboard-driven interface via `agent-init tui`.

## Quick start

```sh
# Run without installing
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --version

# Scaffold a project and seed the default rules
agent-init init path/to/project

# Add a reusable rule and mark it as a default
agent-init rule add be-concise --body "Be concise." --default

# Register a skill source and install a skill
agent-init repo add anthropic https://github.com/anthropics/skills
agent-init skill install anthropic/code-review

# Or use the TUI
agent-init tui
```

## Installation

**Recommended: run with `uvx`** — no local install needed.

```sh
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --version
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init tui
```

**Install permanently as a `uv` tool:**

```sh
uv tool install git+https://github.com/jasperginn/agent-init.git
```

**Local development:**

```sh
git clone https://github.com/jasperginn/agent-init.git
cd agent_init
uv sync
uv run agent-init --version
```

Requires **Python 3.12+**. macOS and Linux are supported. Windows is not supported in this release.

## How it works

### State is split by purpose

Per-project state lives at `.agent-init/manifest.json` and is committed with your repo. It is the source of truth for what is installed where.

Global, machine-local state lives under [platformdirs](https://platformdirs.readthedocs.io/):

| Path | Purpose |
|------|---------|
| `user_data_dir` | SQLite cache of registered repos, indexed skills, templates, and rule metadata. |
| `user_cache_dir/repos/<alias>` | Bare git mirrors reused across projects. |
| `user_cache_dir/snapshots/<alias>/<sha>/<skill>` | Extracted skill bytes used by rollback when the upstream SHA is gone. |
| `user_config_dir/rules` | User-authored rule snippets, one markdown file per rule. |

### Skill discovery

A registered repo must expose at least one skill at one of these paths (precedence high → low):

1. `skills/<name>/SKILL.md`
2. `.claude/skills/<name>/SKILL.md`
3. `<name>/SKILL.md` at repo root
4. `SKILL.md` at repo root — the repo alias becomes the skill name

Skills are referenced everywhere as `<repo_alias>/<skill_name>`. Repos with no discoverable skills are rejected on `repo add` unless you pass `--allow-empty`.

### Versioning

Skill versions are pinned as `<tag>+<short_sha>` when a tag both contains the skill at that revision and is at or after the skill's last-touching commit; otherwise the pin is SHA-only. On `update`, the resolver only attaches the tag when the installed copy honestly reflects it.

## Screenshots

<p align="center">
  <img src="assets/main.png" alt="agent-init TUI main screen" width="720">
</p>
<p align="center"><em>Main TUI screen: navigate repos, skills, rules, and projects from one keyboard-driven interface.</em></p>

<p align="center">
  <img src="assets/rules.png" alt="agent-init TUI rules screen" width="720">
</p>
<p align="center"><em>Rules screen: browse, toggle, and manage the rule snippets that seed every project.</em></p>

<p align="center">
  <img src="assets/skills.png" alt="agent-init TUI skills screen" width="720">
</p>
<p align="center"><em>Skills screen: search registered repos and install version-pinned skills into the current project.</em></p>

## Contributing

Contributions, issues, and questions are welcome. Open an issue to discuss a change before submitting a pull request. See the development commands below for running tests and the TUI locally.

```sh
uv run pytest          # full test suite — 100+ tests, including TUI Pilot + snapshot tests
uv run ruff check .    # lint
uv run agent-init tui  # launch the TUI

pytest tests/tui --snapshot-update  # only after intentional visual changes
```

This project is released under the MIT License.
