<p align="center">
  <img src="assets/logo.png" alt="agent-init logo" width="320">
</p>

# agent-init

A lightweight package manager for your AI-assistant tooling: skills, sub-agents, MCP servers, and rules — version-pinned and tracked in your repo.

## Why this exists

Every AI coding assistant works better with the right context: project conventions, reusable rules, and curated tools. Today that context is scattered across copy-pasted prompts, hand-edited `CLAUDE.md` files, and git submodules nobody wants to maintain.

`agent-init` turns that into a reproducible workflow. It keeps a library of reusable rules, installs versioned skills, agents, and MCP servers from any git repo or registry, and scaffolds the agent instruction file your IDE expects. Everything is recorded in your project so the setup survives a fresh clone.

## Features

- **Generate Karpathy-style `AGENTS.md`** — `init` writes a minimal, opinionated agent instruction file. Project-specific guidance lives in reusable rules, not in `AGENTS.md`.
- **Install skills, agents, and rules from any repo** — register a git URL, browse the index, and install with per-artifact version pinning.
- **Install MCP servers from the community registry** — search the public MCP registry and add servers to `.mcp.json` without hand-editing JSON.
- **A manifest that tells you what you installed** — `.agent-init/manifest.json` is committed to your repo and tracks every skill, agent, MCP server, and rule.
- **Skills that let your agent manage itself** — bundled `repo-add` and `agent-installer` skills let your assistant add sources and install skills/agents/rules straight from a project chat.
- **Hackable profiles** — layout profiles control where skills, rules, and agent files land (e.g. `.claude/`, `.gemini/`, or your own paths).
- **Project templates for common stacks** — save a combo of skills, agents, MCP servers, and rules as a reusable template and bootstrap new projects in seconds.

## Screenshots

The TUI is the default interface; every screenshot below is what you see after running `agent-init`. The skills and agents shown are from repositories that were already registered in the author's workspace — `agent-init` does not ship with a built-in catalog.

### Main menu

<p align="center">
  <img src="assets/main.png" alt="agent-init main menu with keyboard shortcuts for init, repos, skills, agents, MCP, rules, templates, project, config, and profiles" width="720">
</p>

### Browse and install skills

<p align="center">
  <img src="assets/skills.png" alt="Skills browser showing indexed skills from multiple registered repositories with search and install shortcuts" width="720">
</p>

### Browse and install sub-agents

<p align="center">
  <img src="assets/agents.png" alt="Agents browser listing sub-agents with titles, descriptions, and model metadata" width="720">
</p>

### MCP server registry

<p align="center">
  <img src="assets/mcp.png" alt="MCP server registry search screen for discovering community MCP servers" width="720">
</p>

### Reusable rules library

<p align="center">
  <img src="assets/rules.png" alt="Rules library listing named rule snippets with a default flag so chosen rules auto-seed into projects" width="720">
</p>

### Project manifest with drift detection

<p align="center">
  <img src="assets/project.png" alt="Project screen with tabs for installed skills, agents, MCP servers, and rules, showing version pins and drift status" width="720">
</p>

### Layout profiles

<p align="center">
  <img src="assets/profiles.png" alt="Layout profiles screen showing Claude Code, Gemini CLI, and custom profiles with their target directories and mirrors" width="720">
</p>

### Project templates

<p align="center">
  <img src="assets/templates.png" alt="Project templates screen listing reusable setups and the count of skills, agents, MCP servers, and rules each one contains" width="720">
</p>

## Quick start

The default way to use `agent-init` is the TUI. Run it with no arguments:

```sh
agent-init
```

From the main menu you can initialize a project, add repos, search skills/agents/MCP, manage rules, and apply templates — all without leaving the keyboard.

For scripting or CI, the same actions are available as CLI commands:

```sh
# 1. Add a reusable rule and make it a default.
agent-init rule add be-concise --body "Be concise." --default

# 2. Scaffold a project: writes AGENTS.md, mirrors, and seeds default rules.
agent-init init path/to/project

# 3. Register skill/agent/rule source repositories from any git URL.
agent-init repo add anthropic https://github.com/anthropics/skills
agent-init repo add 0xforai https://github.com/0xforai/agents

# 4. Search and install skills, agents, or rules.
agent-init skill search review
agent-init skill install anthropic/code-review
agent-init agent search angular
agent-init agent install 0xforai/angular-expert

# 5. Search and install an MCP server from the registry.
agent-init mcp search fetch
agent-init mcp install fetch

# 6. Update or roll back safely later.
agent-init skill update anthropic/code-review
agent-init skill rollback anthropic/code-review

# 7. Save a reusable project template.
agent-init profile save my-stack path/to/project
agent-init init --template my-stack path/to/new-project
```

## Installation

Requires Python >= 3.12. macOS and Linux are supported; Windows is not supported in v0.1.

Run without installing:

```sh
uvx --from git+https://github.com/jasperginn/agent-init.git agent-init
```

Install permanently as a `uv` tool:

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

## How it works

Per-project state lives at `.agent-init/manifest.json` and is committed to your repo. It pins installed skills, agents, and MCP servers to `(tag, sha, registry_version)` tuples and stores the last 10 versions in `history`, so rollback works even if the upstream repo or registry entry is temporarily unavailable.

Global, machine-local state lives under [platformdirs](https://platformdirs.readthedocs.io/):

- `user_data_dir`: SQLite cache of registered repos, indexed skills/agents, templates, rule metadata, and MCP registry entries.
- `user_cache_dir/repos/<alias>`: bare git mirrors reused across projects.
- `user_cache_dir/snapshots/<alias>/<sha>/<skill>`: extracted artifact bytes used by rollback.
- `user_config_dir/rules`: user-authored rule snippets (one markdown file per rule).

The global SQLite DB is a **cache**. The project's `manifest.json` is the **source of truth** for what is installed where.

### Agent instructions

`init` scaffolds `AGENTS.md` with Karpathy's agent instructions. It is intentionally minimal: project-specific guidance goes into the rules library, not into `AGENTS.md`. Mirrors like `CLAUDE.md` or `GEMINI.md` are symlinks so a single source of truth stays in `AGENTS.md` and the rules stay reusable across projects.

### Skill and agent discovery

A registered repo must expose at least one skill or agent artifact at one of these paths (precedence high to low):

1. `skills/<name>/SKILL.md` or `agents/<name>/AGENT.md`
2. `.claude/skills/<name>/SKILL.md` or `.claude/agents/<name>/AGENT.md`
3. `<name>/SKILL.md` or `<name>/AGENT.md` at repo root
4. `SKILL.md` or `AGENT.md` at repo root (the repo alias becomes the artifact name)

Artifacts are referenced everywhere as `<repo_alias>/<name>`. Repos with no discoverable artifacts are rejected on `repo add` unless you pass `--allow-empty`.

### Versioning

Skill and agent versions are pinned as `<tag>+<short_sha>` when a tag both (a) contains the artifact at that revision and (b) is at or after the artifact's last-touching commit; otherwise the pin is SHA-only. On `update`, the resolver only attaches the tag when the install honestly reflects it. MCP servers are pinned by their registry version.

### Layout profiles

A layout profile decides where installed artifacts land: skills under `.claude/skills/`, rules under `.claude/rules/`, `AGENTS.md` vs `CLAUDE.md` mirrors, and so on. Built-in profiles cover Claude Code and Gemini CLI; you can add your own to match any tool's conventions.

### Project templates

A template captures a combination of profile, default rules, skills, agents, and MCP servers. Applying a template to a new project runs `init` with that profile and then installs everything the template lists, so a team can bootstrap a consistent AI-assistant setup in one command.

### Safety properties

- `repo add` rolls back cleanly on indexing failure: no orphan registrations.
- `git archive | tar` extraction surfaces git's stderr first; `tar` errors are never misattributed.
- Snapshots write a `.agent-init.complete` sentinel; partial extractions are re-run on next access.
- `update` refuses to overwrite hand-edits to the deployed target (compared via `content_hash`); use `--force` to override.
- `init` warns when it overwrites in-region content that was edited by hand since the last write.
- `repo rename` rewrites the SQLite registry and skill index atomically; if the on-disk clone move fails, the DB rename is rolled back.
- Rollback prefers the local snapshot; if both snapshot and upstream are gone, it errors out loudly rather than silently no-op'ing.

## Development

```sh
uv run pytest          # full suite — 100+ tests, including TUI Pilot + snapshot tests
uv run ruff check .    # lint
uv run agent-init      # launch the TUI

pytest tests/tui --snapshot-update  # only after intentional visual changes
```

## Contributing

Issues, ideas, and pull requests are welcome. The project is released under the MIT license.
