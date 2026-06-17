---
name: agent-installer
description: |
  Use whenever the user or another agent asks to install, add, update, delete,
  search for, or list skills, sub-agents, or rules managed by agent-init.
  Covers `agent-init skill`, `agent-init agent`, and `agent-init rule` commands.
  Always prefer the `--compact` output format to keep context small.
---

# Agent Installer

A skill for installing and managing agent-init artifacts: skills, sub-agents, and rules.

## When to use

Use this skill whenever the user (or another skill/agent) asks to:

- Install, add, update, delete, or roll back a skill
- Install, add, update, delete, or roll back a sub-agent
- Install, add, update, delete, or roll back a rule
- Search for or list available skills, agents, or rules

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

2. **Identify the artifact type** from the request:
   - Skills: `agent-init skill ...`
   - Agents: `agent-init agent ...`
   - Rules: `agent-init rule ...`

3. **If the exact name is not known, search first.** Prefer `--compact` NDJSON output to keep token usage low:
   - Skills: `agent-init skill search <query> --compact`
   - Agents: `agent-init agent search <query> --compact`
   - Rules: `agent-init rule list --compact` and filter client-side by the query

4. **Present top matches with `AskUserQuestion`.** If the request is ambiguous or multiple artifacts match, render the top matches as a single-select `AskUserQuestion`. Use the compact NDJSON fields for labels (qualified name and short description). The tool requires `header`, `question`, `type`, and `options` fields; each option must be an object with `label`, `value`, and `description`. If `AskUserQuestion` is unavailable, list the matches in plain text and ask the user to reply with the qualified name.

   Example:
   ```json
   {
     "header": "Select a skill",
     "question": "Which skill do you want to install?",
     "type": "single_select",
     "options": [
       {"label": "repo-add", "value": "agent-init/repo-add", "description": "Register source repositories"},
       {"label": "agent-installer", "value": "agent-init/agent-installer", "description": "Install skills, agents, and rules"}
     ]
   }
   ```

5. **Execute the right command** for the artifact and action:
   - Install: `agent-init skill install <qualified>` / `agent-init agent install <qualified>` / `agent-init rule install <name>`
   - Update: `agent-init skill update <qualified>` / `agent-init agent update <qualified>` / `agent-init rule update <name>`
   - Delete: `agent-init skill delete <qualified>` / `agent-init agent delete <qualified>` / `agent-init rule delete <name>`
   - Rollback: `agent-init skill rollback <qualified>` / `agent-init agent rollback <qualified>` / `agent-init rule rollback <name>`
   - List: `agent-init skill list --compact` / `agent-init agent list --compact` / `agent-init rule list --compact`

6. **Surface CLI warnings verbatim.** If `agent-init` prints warnings about missing prereqs, capability collisions, or local edits, relay them to the user without rewording.

7. **If the artifact is not found,** suggest registering a source repo first using the `repo-add` skill.

## Compact output discipline

For every list or search command, append `--compact` so downstream agents get low-token, structured NDJSON they can parse reliably.