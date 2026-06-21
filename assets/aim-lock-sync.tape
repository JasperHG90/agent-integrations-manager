# aim lock -> aim sync workflow demo for the README.
# Render with: vhs assets/aim-lock-sync.tape
#
# Records against a throwaway copy of this project's aim.toml in /tmp so the real
# repo is never mutated. Prepare it first (policy stripped so the demo output
# stays clean), using the editable .venv build that carries the lock perf fix:
#
#   rm -rf /tmp/aim-demo && mkdir -p /tmp/aim-demo
#   cp aim.toml /tmp/aim-demo/aim.toml
#   sed -i '' '/^\[policy\]/,/^$/d' /tmp/aim-demo/aim.toml
#   (cd /tmp/aim-demo && .venv/bin/aim lock && .venv/bin/aim sync)   # warm caches
#
# With caches warm and the perf fix in place, both lock and sync resolve in
# ~1-2s, so the recording is shown end to end with no post-processing.
Output assets/aim-lock-sync.gif

Require aim

Set Shell "bash"
Set FontSize 22
Set Width 1150
Set Height 640
Set Padding 20
Set WindowBar Colorful
Set Theme "Catppuccin Mocha"
Set WaitTimeout 60s
Set WaitPattern ">$"

# --- silent setup (Hide): fast aim on PATH, clean prompt, reset temp state ---
Hide
Type 'export PATH="/Users/jasperginn/Documents/code_projects/agent_init/.venv/bin:$PATH"' Enter
Type "unset PROMPT_COMMAND" Enter
Type "PS1='\[\e[38;2;137;180;250m\]> \[\e[0m\]'" Enter
Type "cd /tmp/aim-demo && rm -f aim.lock.toml && rm -rf .claude" Enter
Wait
Type "clear" Enter
Wait
Show

# --- the demo ---
Sleep 600ms
Type@70ms "aim lock" Sleep 400ms Enter
Wait
Sleep 1500ms
Type@70ms "aim sync" Sleep 400ms Enter
Wait
Sleep 1200ms
Type@70ms "ls .claude/skills" Sleep 400ms Enter
Wait
Sleep 2500ms
