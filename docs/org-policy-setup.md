# How to set up an org policy

An **org policy** lets a team mandate, from one place, which repos and artifacts (skills,
agents, rules, MCP servers) a project may use, which layout profiles and instruction
archetypes are allowed, and how artifact content is risk-scanned. Projects *bind* to the
policy; CI enforces it against the committed lockfile.

This guide walks through creating the policy repo, binding a project to it, and wiring up the
CI gate.

## Prerequisites

- `aim` installed (`uv tool install git+https://github.com/JasperHG90/agent-integrations-manager.git`).
- A git host where you can create a repo the whole team can read.
- A target project that already has an `aim.toml` (run `aim init` if not).

## 1. Create the org policy repo

An org policy is just a git repo containing **one self-contained `policy.toml`** at its root.
It uses the same sections as the inline `[policy]` table in an `aim.toml`, plus a top-level
`name` and `version`.

Create a new repo (e.g. `acme/policy`) and add `policy.toml`:

```toml
version = 1
name = "acme-baseline"

[repos]
blocked = ["https://github.com/evil/repo"]   # by normalized URL or alias

[artifacts]
blocked_skills = ["somerepo/badskill"]
blocked_agents = ["somerepo/badagent"]
blocked_rules  = ["somerepo/badrule"]
blocked_mcp    = ["somerepo/badmcp"]         # by alias or registry name

[profiles]
allowed = ["claude", "gemini"]               # layout-profile allow-list (empty = all)

[archetypes]
allowed = ["acme/lean"]                       # instruction-archetype allow-list (empty = all)

[risk]
classifier = true          # local ONNX injection/jailbreak screen
llm_judge  = false         # DSPy LLM judge against the rule set
mode = "block"             # "block" (refuse) or "warn" (advisory only)
block_threshold = "high"   # low | medium | high
allow_override = true      # set false to make blocks non-overridable by --override-risk

[[rule]]                   # custom risk rule the judge evaluates against
id = "calls_internal_api"
severity = "medium"
prompt = "Flag if the skill calls our internal admin API without an approval step."
```

Keep only the sections you need — every block above is optional except `version` and `name`.
Commit and push.

> Tip: prototype the policy locally first. In any project run `aim policy init-local` to
> scaffold an inline `[policy]`, edit it, then `aim policy export rules.toml` to pull custom
> `[[rule]]` entries out into a shareable file you can drop into the policy repo.

## 2. Bind a project to the org policy

From the target project root:

```sh
aim policy bind https://github.com/acme/policy
```

This:

- fetches the policy repo, reads `policy.toml`, and warms the local cache, and
- writes `[policy] scope = "org"` (with the repo URL and ref) into the project's `aim.toml`.

Pin to a specific ref if you don't want `HEAD`:

```sh
aim policy bind https://github.com/acme/policy --ref v1.0.0
```

The org policy now **replaces** any local policy for this project.

## 3. Pin it into the lockfile

```sh
aim lock
```

`aim lock` records the policy repo URL, the resolved commit SHA, and a content hash into
`aim.lock.toml`. Commit both `aim.toml` and `aim.lock.toml` — this is what makes the setup
reproducible and what CI checks against.

## 4. Verify locally

```sh
aim policy show       # print the resolved effective policy
aim policy validate   # check declarations + lockfile against the policy (exit 1 on violation)
```

`aim policy validate` walks every declared repo, skill, agent, rule, MCP server, layout
profile, and the pinned policy hash, and fails if anything violates the policy.

## 5. Enforce in CI

The real enforcement boundary is **CI on the committed lockfile**, not the local client. CI
runs the out-of-band gate, which fetches the mandated policy *fresh* (so a developer's local
state can't forge it) and fails the build on any violation:

```sh
aim policy validate --policy https://github.com/acme/policy
```

Add `--ref <tag>` to validate against a pinned policy version. Example GitHub Actions step:

```yaml
- name: Enforce org policy
  run: |
    uvx --from git+https://github.com/JasperHG90/agent-integrations-manager.git \
      aim policy validate --policy https://github.com/acme/policy
```

## How resolution behaves day to day

- **Offline by default.** Resolution reads a cached snapshot — no network call per command.
- **24h TTL.** Once a day, the next command that resolves the policy re-fetches it in the
  background (best-effort, once per process), so bound projects pick up upstream changes
  without anyone running `refresh`.
- **Fails closed.** A project bound to an org policy with **no** usable cache refuses to
  resolve rather than silently downgrading to permissive.

Force an immediate update:

```sh
aim policy refresh
```

## Enforcement points

The policy is checked at three places, so a blocked artifact never lands in a governed
project:

1. `aim lock`
2. install / update (`aim skill add`, `aim agent add`, etc.)
3. the CI gate (`aim policy validate --policy <url>`)

## Risk scanning (optional)

When `[risk].classifier` and/or `[risk].llm_judge` are enabled, artifact content is
classified by *what it instructs*, on top of the always-on hidden-Unicode scan. Install the
extras where scanning runs (locally and/or in CI):

```sh
uv tool install 'aim[risk] @ git+https://github.com/JasperHG90/agent-integrations-manager.git'        # local ONNX injection/jailbreak screen
uv tool install 'aim[risk-judge] @ git+https://github.com/JasperHG90/agent-integrations-manager.git'  # DSPy rule judge
```

With both toggles on, the local screen **gates** the judge: a screen hit blocks immediately
and the judge never runs. Verdicts are cached by content hash, so re-scans are deterministic
and unchanged artifacts aren't re-judged. `mode = "warn"` surfaces findings as advisories
instead of blocking; `--override-risk` overrides a block on add/update unless the policy sets
`allow_override = false`.

## Command reference

| Command | What it does |
| --- | --- |
| `aim policy bind <git-url> [--ref <ref>]` | Point the project at an org policy repo and cache it |
| `aim policy unbind` | Remove `[policy]` (back to permissive) |
| `aim policy refresh` | Re-fetch the bound org policy and update the cache |
| `aim policy show` | Print the resolved effective policy |
| `aim policy validate` | Validate against the effective (bound/local) policy |
| `aim policy validate --policy <git-url> [--ref <ref>]` | Out-of-band CI gate; fetches the policy fresh |
| `aim policy init-local` | Scaffold an inline `[policy]` for local prototyping |
| `aim policy import <file>` | Merge custom `[[rule]]` entries into the inline policy |
| `aim policy export <file>` | Write the policy's custom rules out to a shareable file |
