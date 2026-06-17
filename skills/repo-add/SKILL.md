---
name: repo-add
description: |
  Use when the user asks to register or add a skill source repository,
  rule-library overlay, or agent source repository for atm.
  Handles `atm repo add` and `atm rule-repo add`.
---

# Repo Add

A skill for registering source repositories with atm.

## When to use

Use this skill whenever the user wants to:

- Add a skill/agent/rule source repo to atm
- Register a git URL so skills, agents, or rules become installable
- Refresh or remove a registered repo

## Workflow

1. **Ensure `atm` is installed.** Before running any command, check whether `atm` is available:

   - Try `command -v atm` or `atm --version`.
   - If the command succeeds, continue with the workflow.
   - If the command is not found, tell the user: "`atm` is not installed. Install it with `uv tool install`? Defaults to the latest version; say a version number if you want a specific one."
   - If the user agrees, run:
     - Latest: `uv tool install git+https://github.com/JasperHG90/agent-tooling-manager.git`
     - Specific version: `uv tool install git+https://github.com/JasperHG90/agent-tooling-manager.git@<version>`
   - After installing, verify with `atm --version` before continuing.
   - If the user declines, stop and explain that this skill requires `atm`.

2. **Gather inputs with `AskUserQuestion`.** If the alias, URL, or ref are not already clear from context, ask using `AskUserQuestion`. The tool requires `header`, `question`, `type`, and `options` fields; each option must be an object with `label`, `value`, and `description`. For text inputs, supply example options as quick-select suggestions.

   Example:
   ```json
   [
     {
       "header": "Repository alias",
       "question": "What alias should I use for this repository? (short, lowercase identifier)",
       "type": "text",
       "options": [
         {"label": "local", "value": "local", "description": "Local filesystem alias"},
         {"label": "anth", "value": "anth", "description": "Anthropic-related alias"},
         {"label": "google", "value": "google", "description": "Google-related alias"}
       ]
     },
     {
       "header": "Repository URL",
       "question": "What is the repository URL?",
       "type": "text",
       "options": [
         {"label": "https repo", "value": "https://github.com/user/repo.git", "description": "Public HTTPS git URL"},
         {"label": "ssh repo", "value": "git@github.com:user/repo.git", "description": "SSH git URL"},
         {"label": "local repo", "value": "file:///path/to/repo", "description": "Local filesystem path as file URL"}
       ]
     },
     {
       "header": "Git ref (optional)",
       "question": "Which git ref should I pin? Leave blank for HEAD.",
       "type": "text",
       "options": [
         {"label": "main", "value": "main", "description": "Track the main branch"},
         {"label": "v1.0.0", "value": "v1.0.0", "description": "Pin to a specific release tag"},
         {"label": "develop", "value": "develop", "description": "Track a development branch"}
       ]
     }
   ]
   ```

   If `AskUserQuestion` is unavailable, ask in plain text and confirm the values before running the command.

3. **Prefer `atm repo add`.** This indexes skills, agents, and rules in one operation:

   ```bash
   atm repo add <alias> <url> [--ref <branch-or-tag>]
   ```

4. **If that fails and the user explicitly mentioned rules,** fall back to the rule-library overlay command:

   ```bash
   atm rule-repo add <alias> <url> [--ref <branch-or-tag>]
   ```

5. **After adding, show what became available.** Run one or more of:

   ```bash
   atm skill list --compact
   atm agent list --compact
   atm rule list --compact
   ```

6. **Echo the exact command used** and the short SHA/head if the CLI returned it.

## Tips

- Alias names must be lowercase alphanumeric, `_`, or `-`.
- If the repo contains no skills/agents/rules and registration fails, suggest `--allow-empty` only when the user intentionally wants a placeholder.
- Surface any git authentication hints the CLI provides; do not reword them.
