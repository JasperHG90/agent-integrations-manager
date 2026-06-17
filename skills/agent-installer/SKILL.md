---
name: agent-installer
description: |
  Use whenever the user or another agent asks to install, add, update, delete,
  search for, or list skills, sub-agents, or rules managed by atm.
  Covers `atm skill`, `atm agent`, and `atm rule` commands.
  Always prefer the `--compact` output format to keep context small.
---

# Agent Installer

A skill for installing and managing atm artifacts: skills, sub-agents, and rules.

## When to use

Use this skill whenever the user (or another skill/agent) asks to:

- Install, add, update, delete, or roll back a skill
- Install, add, update, delete, or roll back a sub-agent
- Install, add, update, delete, or roll back a rule
- Search for or list available skills, agents, or rules

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

2. **Identify the artifact type** from the request:
   - Skills: `atm skill ...`
   - Agents: `atm agent ...`
   - Rules: `atm rule ...`

3. **If the exact name is not known, search first.** Prefer `--compact` NDJSON output to keep token usage low:
   - Skills: `atm skill search <query> --compact`
   - Agents: `atm agent search <query> --compact`
   - Rules: `atm rule list --compact` and filter client-side by the query

4. **Present top matches with `AskUserQuestion`.** If the request is ambiguous or multiple artifacts match, render the top matches as a single-select `AskUserQuestion`. Use the compact NDJSON fields for labels (qualified name and short description). The tool requires `header`, `question`, `type`, and `options` fields; each option must be an object with `label`, `value`, and `description`. If `AskUserQuestion` is unavailable, list the matches in plain text and ask the user to reply with the qualified name.

   Example:
   ```json
   {
     "header": "Select a skill",
     "question": "Which skill do you want to install?",
     "type": "single_select",
     "options": [
       {"label": "repo-add", "value": "atm/repo-add", "description": "Register source repositories"},
       {"label": "agent-installer", "value": "atm/agent-installer", "description": "Install skills, agents, and rules"}
     ]
   }
   ```

5. **Execute the right command** for the artifact and action:
   - Install: `atm skill install <qualified>` / `atm agent install <qualified>` / `atm rule install <name>`
   - Update: `atm skill update <qualified>` / `atm agent update <qualified>` / `atm rule update <name>`
   - Delete: `atm skill delete <qualified>` / `atm agent delete <qualified>` / `atm rule delete <name>`
   - Rollback: `atm skill rollback <qualified>` / `atm agent rollback <qualified>` / `atm rule rollback <name>`
   - List: `atm skill list --compact` / `atm agent list --compact` / `atm rule list --compact`

6. **Surface CLI warnings verbatim.** If `atm` prints warnings about missing prereqs, capability collisions, or local edits, relay them to the user without rewording.

7. **If the artifact is not found,** suggest registering a source repo first using the `repo-add` skill.

## Compact output discipline

For every list or search command, append `--compact` so downstream agents get low-token, structured NDJSON they can parse reliably.
