<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="480">
</p>

# agent-init

A CLI and TUI for scaffolding agent-engineering projects, managing reusable rules, and installing Claude skills from git repositories.

## Why this exists

Every new AI-assisted project needs the same groundwork: an `AGENTS.md` file, editor-specific mirrors, rule snippets, and a curated set of skills. Setting that up by hand is repetitive and quickly drifts out of sync. `agent-init` turns that into a reproducible, versioned workflow so your agent context is correct from day one and stays correct as upstream skills evolve.

## Features

- **Scaffold agent projects** — generate `AGENTS.md` plus mirrors like `CLAUDE.md` or `GEMINI.md` from a single managed template.
- **Reusable rules** — write rule snippets once, mark defaults, and auto-seed them into every new project.
- **Skill management from git** — register skill repositories, search across them, install skills into projects, update them, and roll back to any previous version.
- **Project-first state** — every install is pinned in `.agent-init/manifest.json`, so the project itself is the source of truth, not a local database.
- **Interactive TUI** — browse repos, skills, rules, and project templates without memorizing CLI flags.

<p align="center">
  <img src="assets/demo.gif" alt="agent-init TUI walkthrough" width="640">
</p>

## Quick start

```sh
# 1. Run with uvx (no permanent install)
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --version

# 2. Add a reusable rule and make it a default
agent-init rule add be-concise --body "Be concise." --default

# 3. Scaffold a project with AGENTS.md + CLAUDE.md + GEMINI.md
agent-init init path/to/project

# 4. Register a skill source repo and install a skill
agent-init repo add anthropic https://github.com/anthropics/skills
agent-init skill install anthropic/code-review

# 5. Or launch the interactive TUI
agent-init tui
```

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

Recommended: run directly with `uvx`:

```sh
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init --help
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

## How it works

- `init` writes a managed `AGENTS.md` and optional editor mirrors. Marked regions inside those files are regenerated later without clobbering hand-written content.
- `rule add --default` pins a rule snippet so every `init` seeds it into the new project.
- `repo add` clones and indexes a skill repository. `skill install` extracts the skill into the project and records the exact `(tag, sha)` pair in `.agent-init/manifest.json`.
- `skill update` and `skill rollback` move between versions while keeping a local history, so rollback works even on a fresh clone.

## Contributing

Issues and PRs are welcome. The project uses `ruff` for linting and `pytest` for tests, including Textual TUI snapshot tests.

```sh
uv run pytest
uv run ruff check .
```

Licensed under MIT.
