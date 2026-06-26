# Justfile for atm — Agent Tooling Manager.
# Install `just`: https://github.com/casey/just

set positional-arguments

# Default recipe: show available commands.
default:
    @just --list

# Sync dependencies and install the project in the local venv.
install:
    uv sync --all-groups --all-extras
    uv run prek install

# Run the full pytest suite.
test *args='':
    uv run pytest {{args}}

# Run only core tests (fast, no TUI snapshots).
test-core:
    uv run pytest tests/core -q

# Run only TUI tests.
test-tui *args='':
    uv run pytest tests/tui {{args}}

# Update TUI snapshots after intentional visual changes.
snapshot-update:
    uv run pytest tests/tui --snapshot-update

# Run the linter.
lint:
    uv run ruff check src tests

# Run the linter with auto-fix.
lint-fix:
    uv run ruff check --fix src tests

# Format all Python code.
format:
    uv run ruff format src tests

# Run mypy type checks.
typecheck:
    uv run mypy src tests \
        --ignore-missing-imports \
        --check-untyped-defs \
        --exclude 'tests/integration/test_app.py'

# Run lint + typecheck + tests. Handy for pre-push.
check: lint typecheck test-core

# Run the atm TUI locally.
tui *args='':
    uv run atm tui {{args}}

# Run atm in the current project.
run *args='':
    uv run atm {{args}}

# Build distribution packages.
build:
    uv build --all-packages

# Run pre-commit on all files.
pre-commit:
    uvx pre-commit run --all-files

# Clean generated artifacts.
clean:
    rm -rf dist/ .pytest_cache/ .ruff_cache/ .mypy_cache/
