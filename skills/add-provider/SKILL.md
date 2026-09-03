---
name: add-provider
description: |
  Use when the user wants aim to support a new agent client / provider —
  Cursor, opencode, Windsurf, Codex, Zed, or any tool that reads project
  instructions or installs plugins. Covers creating a layout profile (where
  skills, rules, subagents, and the AGENTS.md mirror land), authoring a
  plugin-target TOML (how the client's plugins are discovered and installed),
  and sharing both with teammates. Reach for this whenever someone says
  "make aim work with X", "add X support", "install skills for X", or asks
  where a client's instruction file or plugins should go — even if they don't
  use the word "provider".
---

# Add Provider

Wire a new agent client ("provider") into a project managed by aim. A provider
needs at most two artifacts, both plain TOML, no aim source changes:

- a **layout profile** — WHERE aim installs skills, rules, subagents, the
  AGENTS.md mirror file, and MCP config for this client;
- a **target** (plugin kind) — HOW aim discovers and installs this client's
  plugins, if the client has a plugin ecosystem. Skip it otherwise.

## When to use

- "Make aim install skills/rules for Cursor / opencode / Windsurf / <client>"
- "Where should <client>'s instruction file go?"
- "Teach aim to install <client> plugins"
- "Share our <client> setup with the team"

## Workflow

1. **Ensure `aim` is installed.** Try `aim --version`. If missing, offer:
   `uv tool install git+https://github.com/JasperHG90/agent-integrations-manager.git`
   and verify with `aim --version` before continuing. If the user declines,
   stop — this skill drives the aim CLI.

2. **Learn how the client consumes agent tooling.** Answer these from the
   client's docs (WebSearch/WebFetch if available, otherwise ask the user):
   - Which instruction file does it read? (`AGENTS.md` natively, or its own
     name like `CLAUDE.md` / `GEMINI.md`?)
   - Does it read per-file rules from a directory, or only one instruction
     file? (drives `rules_mode`: `files` vs `inline`)
   - Where does it expect skills / subagents, if supported?
   - Does it have a plugin format? What is a plugin's manifest file
     (e.g. `package.json`, `extension.json`), and where do installed plugins
     live in a project?

   Getting these right matters more than speed: the profile is checked into
   the repo and every teammate's sync follows it.

3. **Write the layout profile** at `.aim/layout-profiles/<name>.toml` in the
   project (lowercase name; `claude` and `gemini` are reserved built-ins):

   ```toml
   name = "cursor"
   display_name = "Cursor"
   description = "Install paths for Cursor."
   # project = this repo only; global = cached in the user DB + read-only repo copy
   scope = "project"
   # "files" writes each rule to rules_dir; "inline" renders rules into AGENTS.md
   # (use inline when the client reads only the single instruction file)
   rules_mode = "files"

   rules_dir = ".cursor/rules"
   skills_dir = ".cursor/skills"
   agents_dir = ".cursor/agents"
   agents_md = "AGENTS.md"
   mcp_json = ".mcp.json"
   # Mirror filenames symlinked to AGENTS.md, for clients that read their own name
   symlinks = ["CURSOR.md"]
   ```

   Rules for the fields: all paths are relative and descending-only (no `..`,
   no absolute paths); `agents_md` must not also appear in `symlinks`. Keep
   `agents_md = "AGENTS.md"` and add the client's filename to `symlinks` —
   one canonical instruction file, mirrored per client, is the whole point.

   Constraint: each `symlinks` entry must be a `*.md` filename starting with a
   letter or digit (e.g. `CURSOR.md`; aim rejects names like `.cursorrules` or
   `.windsurfrules`). When the client's instruction file is not a legal mirror
   name, either configure the client to read `AGENTS.md` directly (many
   clients support this — check first), or set `agents_md` to the client's
   filename itself (it is a path field, validated more loosely) and accept
   that the canonical file is then no longer named AGENTS.md. Say which
   trade-off you chose and why.

4. **Activate and render it:**

   ```bash
   aim init <project> --profile <name>   # records it in aim.toml and renders
   aim lock && aim sync                  # resolve + apply
   ```

   Verify: the directories from the profile exist after installing an
   artifact, and the symlink (e.g. `CURSOR.md -> AGENTS.md`) is present.

5. **Author a target only if the client has plugins.** A target is a
   declarative plugin kind: discovery is anchored on a JSON manifest file
   (any repo directory containing it is a plugin; the whole directory gets
   vendored), registration says where the bytes land. Write
   `targets/<name>.toml` in a shared source repo (or `.aim/targets/` in the
   project for local-only use):

   ```toml
   name = "cursor"

   [manifest]
   # A cursor plugin is a directory with this JSON file; aim reads the
   # plugin's name from the "name" keypath and vendors the directory.
   file = "extension.json"
   name = "name"                 # dotted keypath to the plugin name
   # description = "description" # optional keypath
   # version = "version"         # optional keypath

   [register]
   # Destination template; only {name} and {repo} are interpolated.
   vendor_into = ".cursor/extensions/{name}"

   # Optional: patch a client config file on install/uninstall.
   # [[register.config]]
   # file = ".cursor/config.json"
   # format = "json"
   # [register.config.set]
   # "extensions.{name}.enabled" = true
   ```

   The manifest must be JSON. `vendor_into` and `config.file` must be safe
   relative paths. Targets are config, not executable code — that is why a
   repo or teammate can ship one safely.

6. **Install the target into the project:**

   ```bash
   # from a repo that ships targets/<name>.toml (registers the repo if needed):
   aim target add <git-url-or-alias/name> --yes
   # then verify:
   aim target list
   ```

   This vendors the TOML into `.aim/targets/<name>.toml` and SHA-pins it in
   `aim.lock.toml`. Reindex source repos (`aim repo reindex <alias>`) so
   plugins of the new kind become discoverable, then `aim plugin list`.

7. **Share it.** Commit `.aim/layout-profiles/<name>.toml`, `.aim/targets/`,
   `aim.toml`, and `aim.lock.toml`. Teammates run `aim sync` and get the
   provider with no extra steps. To offer the target to OTHER projects, ship
   it in a source repo under `targets/` — it becomes installable via
   `aim target add` for anyone who registers that repo.

## Tips

- One provider, two axes: profile = core artifacts, target = plugins. A
  client without plugins needs only the profile.
- `aim sync --profile <name>` overrides the manifest for a one-off render;
  prefer `aim init --profile` to persist the choice.
- Existing example: `examples/targets/opencode.toml` in the aim repo is the
  reference pluggable kind (npm-style `package.json` discovery).
- Echo the exact commands you ran and surface any risk/policy warnings the
  CLI prints; do not reword them.
