# Sharing project templates

A **project template** is a reusable bundle of a project's instruction
template, layout, symlinks, rules, skills, sub-agents, and MCP servers. You
can save one, share it as a TOML file or from a git repo, and stamp new
projects from it. `aim` provides the commands. You wire updates into your own
CI.

A template freezes every artifact to the exact commit `sha` it resolved to in
the source project's lock, so applying it reproduces identical versions on any
machine that has the source repos registered. The template's content hash is a
complete fingerprint of that resolved bundle.

## Save and share a single file

Lock the project (so artifacts have resolved SHAs to freeze), then snapshot and
export it:

```bash
aim lock
aim template save my-template .
aim template export my-template my-template.toml
```

The TOML records a `[[repo]]` block with the url of every source repo the
template's artifacts come from. Import it elsewhere:

```bash
aim template import my-template.toml --name my-template
aim template apply my-template .
```

`apply` matches the template's source repos by url. A repo already registered
under a different alias is reused (the template's aliases are rewritten to your
local ones); a repo you don't have yet is cloned from the url the template
records. A locally-saved template applies leniently: artifacts it still cannot
resolve are skipped.

## Host templates in an org repo

Commit template TOML files under `templates/` (or `.aim/templates/`) in a git
repo:

```
acme/templates
  templates/python-service.toml
  templates/data-pipeline.toml
```

Register the repo and apply a template by its qualified name:

```bash
aim repo add https://github.com/acme/templates.git
aim template list --repo templates
aim template apply templates/python-service .
```

`apply` auto-registers the source repos the template's artifacts come from,
cloning each url recorded in the template's `[[repo]]` block. Every clone and
every artifact install passes the project's effective policy gate, so a blocked
repo url is refused and org allow-lists bind to template-sourced artifacts.

## Lock and drift

`aim lock` records the applied template in `aim.lock.toml`: the source repo url,
the qualified template name, the resolved commit SHA, and the template content
hash. Because the template already embeds each artifact's exact `sha`, that one
hash fingerprints the whole resolved bundle.

`aim template check` compares the applied template hash against the upstream
template and sets its exit code so a pipeline can branch on it:

| Exit | Meaning |
|------|---------|
| 0 | Up to date (or the project was not stamped from a template) |
| 2 | The upstream template changed since it was applied |

```bash
aim template check --json        # machine-readable report
aim template diff                # preview added/removed template members
aim template update              # converge: add new, remove dropped, re-lock
```

`aim template update` removes template-owned artifacts that upstream dropped,
installs new ones, re-applies the rest at their new SHAs, and re-locks.
Artifacts you added on top of the template are left untouched.

## Maintaining a template you publish

A template pins each artifact to an exact `sha`. To move those pins forward
(authored by you, applied by consumers), bump the SHAs in the template itself:

```bash
aim template bump my-template            # re-resolve every artifact to its latest SHA
aim template bump my-template acme/foo   # bump just one artifact
```

`bump` auto-registers the source repos from the template's `[[repo]]` block,
resolves each artifact's newest commit, and rewrites its `sha`. It edits the
saved template, so commit the change (or `export` it back into your template
repo) for consumers to pick up on their next `aim template update`.

## Wire updates into your own CI

`aim` supplies the commands. Your pipeline owns the workflow. The example
below polls for template drift on a schedule and opens a pull request when an
update is available. It patterns on the org-policy gate composite action in
`.github/actions/aim/action.yml`.

```yaml
name: template-update
on:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch: {}

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: Install aim
        run: uv tool install git+https://github.com/JasperHG90/agent-integrations-manager.git
      - name: Refresh source repos
        run: aim repo refresh
      - name: Check for template drift
        id: check
        run: aim template check --json > check.json || echo "drift=1" >> "$GITHUB_OUTPUT"
      - name: Update and open a PR
        if: steps.check.outputs.drift == '1'
        run: |
          aim template update
        # then commit the changed aim.toml / aim.lock.toml and open a PR with
        # your tool of choice (e.g. peter-evans/create-pull-request).
```

`aim` does not open the pull request or run the pipeline. It detects drift
(`check`), previews the change (`diff`), and converges the project
(`update`). The surrounding automation is yours.
