<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="480">
</p>

# agent-init

A small CLI and TUI that scaffolds agent-engineering projects, manages reusable rules, and installs Claude skills from git repositories.

## Why this exists

Every AI-assisted project needs the same groundwork: an `AGENTS.md` file that tells assistants how to behave, a set of rules you actually reuse, and a way to pull in curated skills without copy-pasting Markdown. Setting that up by hand is repetitive, and it drifts the moment someone on the team updates a rule.

`agent-init` turns that groundwork into a single workflow. It generates project instructions from a managed template, keeps a library of reusable rule snippets, and installs, versions, and rolls back Claude skills straight from any git repo.

## Features

- **Scaffold agent instructions in seconds.** Generates `AGENTS.md` plus agent-specific mirrors like `CLAUDE.md` and `GEMINI.md`.
- **Reuse rules across projects.** Maintain rule snippets in one place and seed the ones you mark as default into every new project.
- **Install skills from git.** Register any git repository, browse or search its skills, and install them with version pinning and rollback.
- **Rollback without guessing.** Per-project `manifest.json` pins skills to a `(tag, sha)` pair and keeps the last 10 versions, so rollback works even on a fresh clone.
- **TUI or CLI.** Use `agent-init tui` for browsing, or script the same commands in CI.

## Quick start

```sh
# Add a reusable rule and make it seed into every new project
agent-init rule add be-concise --body "Be concise." --default

# Scaffold a new project
agent-init init path/to/project

# Register a skill source repo
agent-init repo add anthropic https://github.com/anthropics/skills

# Install a skill
agent-init skill install anthropic/code-review
```

Or launch the TUI:

```sh
agent-init tui
```

## Installation

Requires Python 3.12+ on macOS or Linux. Windows is not supported in this release.

**Run without installing, using `uvx`:**

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
cd agent-init
uv sync
uv run agent-init --version
```

## How to use

<p align="center">
  <img src="assets/demo.gif" alt="agent-init TUI demo showing project initialization, rule management, and skill installation" width="640">
</p>

### Scaffolding a project

Initialize a directory with `AGENTS.md` and agent-specific mirrors:

```sh
agent-init init path/to/project
```

### Managing rules

Add a rule snippet and mark it as default so it seeds into every new project:

```sh
agent-init rule add be-concise --body "Be concise." --default
```

### Installing skills

Register a source repo, then search and install skills:

```sh
agent-init repo add anthropic https://github.com/anthropics/skills
agent-init skill search review
agent-init skill install anthropic/code-review
```

Keep skills up to date, or roll back when something breaks:

```sh
agent-init repo refresh anthropic
agent-init skill update anthropic/code-review
agent-init skill rollback anthropic/code-review
```

### How it works

Per-project state lives at `.agent-init/manifest.json` and is committed with your repo. The manifest pins installed skills to a `(tag, sha)` pair and keeps the last 10 versions so rollback survives fresh clones.

Global state is machine-local and treated as a cache:

- `user_data_dir`: SQLite registry of repos, indexed skills, templates, and rule metadata.
- `user_cache_dir/repos/<alias>`: bare git mirrors reused across projects.
- `user_cache_dir/snapshots/<alias>/<sha>/<skill>`: extracted skill bytes used when the upstream SHA is no longer reachable.
- `user_config_dir/rules`: user-authored rule snippets, one Markdown file per rule.

Skills are discovered at these paths, in order of precedence:

1. `skills/<name>/SKILL.md`
2. `.claude/skills/<name>/SKILL.md`
3. `<name>/SKILL.md` at repo root
4. `SKILL.md` at repo root (the repo alias becomes the skill name)

They are referenced everywhere as `<repo_alias>/<skill_name>`.

## Contributing

Open an issue to report bugs or request features. Pull requests are welcome; please keep changes focused and include tests where possible. See the repository's `CONTRIBUTING.md` if available, and join existing discussions for larger design questions.

This project is licensed under the terms specified in `LICENSE`.
