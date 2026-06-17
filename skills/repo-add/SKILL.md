---
name: repo-add
description: |
  Use when the user asks to register or add a skill source repository,
  rule-library overlay, or agent source repository for agent-init.
  Handles `agent-init repo add` and `agent-init rule-repo add`.
---

# Repo Add

A skill for registering source repositories with agent-init.

## When to use

Use this skill whenever the user wants to:

- Add a skill/agent/rule source repo to agent-init
- Register a git URL so skills, agents, or rules become installable
- Refresh or remove a registered repo

## Workflow

1. **Ensure `agent-init` is installed.** Before running any command, check whether `agent-init` is available:

   - Try `command -v agent-init` or `agent-init --version`.
   - If the command succeeds, continue with the workflow.
   - If the command is not found, tell the user: "`agent-init` is not installed. Install it with `uvx`? Defaults to the latest version; say a version number if you want a specific one."
   - If the user agrees, run:
     - Latest: `uvx install agent-init`
     - Specific version: `uvx install agent-init==<version>`
   - After installing, verify with `agent-init --version` before continuing.
   - If the user declines, stop and explain that this skill requires `agent-init`.

2. **Gather inputs** from context or by asking the user:
   - Local alias (short, lowercase, e.g. `local`, `anth`, `google`)
   - Git URL (https, ssh, or `file://`)
   - Optional default ref (default: `HEAD`)

3. **Prefer `agent-init repo add`.** This indexes skills, agents, and rules in one operation:

   ```bash
   agent-init repo add <alias> <url> [--ref <branch-or-tag>]
   ```

4. **If that fails and the user explicitly mentioned rules,** fall back to the rule-library overlay command:

   ```bash
   agent-init rule-repo add <alias> <url> [--ref <branch-or-tag>]
   ```

5. **After adding, show what became available.** Run one or more of:

   ```bash
   agent-init skill list --compact
   agent-init agent list --compact
   agent-init rule list --compact
   ```

6. **Echo the exact command used** and the short SHA/head if the CLI returned it.

## Tips

- Alias names must be lowercase alphanumeric, `_`, or `-`.
- If the repo contains no skills/agents/rules and registration fails, suggest `--allow-empty` only when the user intentionally wants a placeholder.
- Surface any git authentication hints the CLI provides; do not reword them.