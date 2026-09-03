"""Thin shell-out wrapper around `git`. No GitPython — fewer deps, less surface.

Cached clones are bare and live at `user_cache_dir/repos/<alias>/`. We never
check out against these clones; reads use `git -C <bare> show`, `ls-tree`,
`for-each-ref`, and `archive`, all of which work fine on bare repos.

The module exposes a `GitBackend` protocol and a `RealGitBackend` shell-out
impl. Tests inject their own backend (or use the shell-out one against a
local fixture bare repo).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class GitError(RuntimeError):
    """Raised when a `git` (or `tar`) invocation fails or is unavailable."""


# Disable interactive credential prompts so a misconfigured remote can't hang
# the CLI/TUI indefinitely. Also set a generous hard ceiling on all git ops.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false"}
_GIT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class TagInfo:
    """A git tag paired with the object SHA it points at."""

    name: str
    sha: str


class GitBackend(Protocol):
    """Read/fetch interface over a bare git clone; tests inject fakes."""

    def clone_bare(self, url: str, dest: Path) -> None:
        """Clone `url` into `dest` as a bare/mirror repo."""
        ...

    def fetch(self, repo_dir: Path) -> None:
        """Fetch updated refs and tags into the bare clone at `repo_dir`."""
        ...

    def resolve_ref(self, repo_dir: Path, ref: str) -> str:
        """Resolve `ref` to its full object SHA."""
        ...

    def list_tags(self, repo_dir: Path) -> list[TagInfo]:
        """List all tags in the repo with their target SHAs."""
        ...

    def latest_tag(self, repo_dir: Path, ref: str) -> str | None:
        """Return the most recent tag reachable from `ref`, or None."""
        ...

    def ls_tree(self, repo_dir: Path, sha: str, path: str = "") -> list[str]:
        """List file paths in the tree at `sha`, optionally under `path`."""
        ...

    def cat_file(self, repo_dir: Path, sha: str, path: str) -> str:
        """Read the blob at `sha:path` as decoded text."""
        ...

    def cat_file_bytes(self, repo_dir: Path, sha: str, path: str) -> bytes:
        """Read the blob at `sha:path` as raw bytes."""
        ...

    def cat_file_batch(self, repo_dir: Path, sha: str, paths: list[str]) -> dict[str, bytes]:
        """Read many blobs at `sha` in one pass, keyed by path."""
        ...

    def archive(self, repo_dir: Path, sha: str, source_path: str, dest_dir: Path) -> None:
        """Extract the `source_path` subtree at `sha` into `dest_dir`."""
        ...

    def last_touching_sha(self, repo_dir: Path, ref: str, source_path: str) -> str:
        """Return the SHA of the last commit touching `source_path`."""
        ...

    def default_branch(self, repo_dir: Path) -> str | None:
        """Return the remote's default branch name (e.g. `main`), or None."""
        ...

    def commits_behind(self, repo_dir: Path, ref: str, base: str) -> int:
        """Count commits reachable from `base` but not from `ref`."""
        ...


def _run(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout: int | None = None,
) -> bytes:
    """Run a git/tar command and return its stdout, mapping failures to GitError.

    Args:
        args: The full argv to execute.
        cwd: Working directory for the subprocess.
        input_bytes: Optional bytes piped to the process's stdin.
        timeout: Hard ceiling in seconds; falls back to the module default.

    Returns:
        The process's stdout bytes.

    Raises:
        GitError: If the executable is missing, exits non-zero, or times out.
    """
    try:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            input=input_bytes,
            check=True,
            capture_output=True,
            env=_GIT_ENV,
            timeout=timeout or _GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise GitError("`git` executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {exc.timeout}s") from exc
    return result.stdout


class RealGitBackend:
    """Default `GitBackend` that shells out to the `git` executable."""

    def clone_bare(self, url: str, dest: Path) -> None:
        """Clone as a `--mirror` (bare + auto-configured `+refs/*:refs/*` refspec).

        `--bare` alone leaves the fetch refspec empty, so `fetch origin` is a
        no-op. `--mirror` is also bare, but sets things up so subsequent
        `fetch --tags --prune` mirrors the remote into the cache.

        We deliberately do a FULL clone (no `--filter=blob:none`): aim's hot path
        is hashing the blob content of declared artifacts, so a blobless partial
        clone just defers each blob into a separate on-demand fetch round-trip —
        measured ~3x slower end-to-end for a cold lock than fetching all blobs
        once at clone time.
        """
        if dest.exists():
            raise GitError(f"clone dest already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # `--` ensures a URL/ref starting with `-` is not parsed as a git option.
        _run(["git", "clone", "--mirror", "--quiet", "--", url, str(dest)])

    def fetch(self, repo_dir: Path) -> None:
        """Mirror the remote's refs and tags into the bare clone, pruning deletions."""
        _run(["git", "-C", str(repo_dir), "fetch", "--quiet", "--tags", "--prune", "origin"])

    def resolve_ref(self, repo_dir: Path, ref: str) -> str:
        """Resolve `ref` to its full object SHA.

        Raises:
            GitError: If `ref` starts with '-' (would be parsed as an option).
        """
        if ref.startswith("-"):
            raise GitError(f"ref must not start with '-': {ref!r}")
        out = _run(["git", "-C", str(repo_dir), "rev-parse", ref])
        return out.decode().strip()

    def list_tags(self, repo_dir: Path) -> list[TagInfo]:
        """List all tags in the repo with their target SHAs."""
        out = _run(
            [
                "git",
                "-C",
                str(repo_dir),
                "for-each-ref",
                "--format=%(refname:strip=2) %(objectname)",
                "refs/tags",
            ]
        )
        tags: list[TagInfo] = []
        for line in out.decode().splitlines():
            if not line.strip():
                continue
            name, sha = line.split(" ", 1)
            tags.append(TagInfo(name=name, sha=sha))
        return tags

    def latest_tag(self, repo_dir: Path, ref: str) -> str | None:
        """Return the most recent tag reachable from `ref`, or None if none exist.

        Raises:
            GitError: If `ref` starts with '-' (would be parsed as an option).
        """
        if ref.startswith("-"):
            raise GitError(f"ref must not start with '-': {ref!r}")
        try:
            out = _run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "describe",
                    "--tags",
                    "--abbrev=0",
                    ref,
                ]
            )
        except GitError:
            return None
        name = out.decode().strip()
        return name or None

    def ls_tree(self, repo_dir: Path, sha: str, path: str = "") -> list[str]:
        """List file paths recursively in the tree at `sha`, optionally under `path`."""
        args = ["git", "-C", str(repo_dir), "ls-tree", "-r", "--name-only", sha]
        if path:
            args += ["--", path]
        out = _run(args)
        return [line for line in out.decode().splitlines() if line]

    def cat_file(self, repo_dir: Path, sha: str, path: str) -> str:
        """Read the blob at `sha:path` as decoded text."""
        out = _run(["git", "-C", str(repo_dir), "show", f"{sha}:{path}"])
        return out.decode()

    def cat_file_bytes(self, repo_dir: Path, sha: str, path: str) -> bytes:
        """Read the blob at `sha:path` as raw bytes."""
        return _run(["git", "-C", str(repo_dir), "show", f"{sha}:{path}"])

    def cat_file_batch(self, repo_dir: Path, sha: str, paths: list[str]) -> dict[str, bytes]:
        """Read many blobs at `sha` with one `git cat-file --batch` process.

        Avoids the per-file fork/exec of `cat_file_bytes` when hashing a whole
        skill tree. Responses come back in request order, so we map them onto the
        input `paths` positionally rather than by the echoed object name.
        """
        if not paths:
            return {}
        request = b"".join(f"{sha}:{p}\n".encode() for p in paths)
        out = _run(["git", "-C", str(repo_dir), "cat-file", "--batch"], input_bytes=request)
        result: dict[str, bytes] = {}
        i = 0
        for path in paths:
            nl = out.index(b"\n", i)
            header = out[i:nl].decode()  # "<obj> blob <size>" or "<input> missing"
            i = nl + 1
            parts = header.split(" ")
            if parts[-1] == "missing":
                raise GitError(f"object missing: {sha}:{path}")
            size = int(parts[2])
            result[path] = out[i : i + size]
            i += size + 1  # skip the trailing newline git emits after the blob
        return result

    def archive(self, repo_dir: Path, sha: str, source_path: str, dest_dir: Path) -> None:
        """Extract the `source_path` subtree at `sha` into `dest_dir`, flattened.

        The contents of `source_path/` land directly under `dest_dir`.

        Implementation: run `git archive` to bytes first (catching git failures
        cleanly), then feed those bytes to `tar -x`. Avoids a pipe deadlock and
        ensures git's stderr surfaces instead of being shadowed by tar's
        "empty input" error.
        """
        # The sha slot is the only argv position here git would parse as an
        # option; it can come from a teammate-supplied lockfile, so guard it
        # like resolve_ref guards refs (the model validates the format too).
        if sha.startswith("-"):
            raise GitError(f"sha must not start with '-': {sha!r}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Empty source_path means the whole repo root is the skill.
        path_spec = source_path or "."
        try:
            archive_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "archive",
                    "--format=tar",
                    sha,
                    "--",
                    path_spec,
                ],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise GitError("`git` executable not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace").strip()
            raise GitError(f"git archive failed: {stderr}") from exc

        try:
            strip_components = source_path.count("/") + 1 if source_path else 0
            tar_result = subprocess.run(
                [
                    "tar",
                    "-x",
                    "-C",
                    str(dest_dir),
                    f"--strip-components={strip_components}",
                    "--no-same-owner",
                    "--no-same-permissions",
                    "--no-acls",
                ],
                input=archive_result.stdout,
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise GitError("`tar` executable not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace").strip()
            raise GitError(f"tar failed: {stderr}") from exc
        _ = tar_result

    def last_touching_sha(self, repo_dir: Path, ref: str, source_path: str) -> str:
        """Return the SHA of the last commit reachable from `ref` touching `source_path`.

        Args:
            source_path: Subtree path; falls back to "SKILL.md" when empty.

        Raises:
            GitError: If `ref` starts with '-', or no commit touches the path.
        """
        if ref.startswith("-"):
            raise GitError(f"ref must not start with '-': {ref!r}")
        path_spec = source_path or "SKILL.md"
        out = _run(
            [
                "git",
                "-C",
                str(repo_dir),
                "log",
                "-1",
                "--format=%H",
                ref,
                "--",
                path_spec,
            ]
        )
        sha = out.decode().strip()
        if not sha:
            raise GitError(f"no commits touch {path_spec} reachable from {ref}")
        return sha

    def default_branch(self, repo_dir: Path) -> str | None:
        """Return the remote's default branch (e.g. `main`), or None if undetermined.

        A ``--mirror`` clone mirrors the remote's symbolic HEAD, so the bare clone's
        HEAD points at the remote's default branch. Returns None when HEAD is detached
        or unresolvable (e.g. an empty remote).
        """
        try:
            out = _run(["git", "-C", str(repo_dir), "symbolic-ref", "--short", "HEAD"])
        except GitError:
            return None
        name = out.decode().strip()
        return name or None

    def commits_behind(self, repo_dir: Path, ref: str, base: str) -> int:
        """Count commits reachable from `base` but not from `ref` (`ref..base`).

        This is how many commits `ref` is missing relative to `base` — the "behind"
        distance. Raises GitError if either ref starts with '-' or does not resolve.
        """
        for name in (ref, base):
            if name.startswith("-"):
                raise GitError(f"ref must not start with '-': {name!r}")
        out = _run(["git", "-C", str(repo_dir), "rev-list", "--count", f"{ref}..{base}"])
        return int(out.decode().strip() or "0")


def remove_clone(repo_dir: Path) -> None:
    """Delete the bare clone at `repo_dir` if it exists."""
    if repo_dir.exists():
        shutil.rmtree(repo_dir)


_default_backend: GitBackend = RealGitBackend()


def get_backend() -> GitBackend:
    """Return the active git backend."""
    return _default_backend


def cat_files_text(repo_dir: Path, sha: str, paths: list[str]) -> dict[str, str]:
    """Read many blobs at `sha` as decoded text in a single batched git process.

    Collapses the per-file fork/exec of `cat_file` into one `cat-file --batch`,
    which is the dominant cost when indexing a repo's whole catalog. On batch
    failure (e.g. a missing object) it falls back to per-file reads and skips any
    path it cannot read, matching the indexer's tolerance of unreadable files.
    """
    if not paths:
        return {}
    backend = get_backend()
    try:
        raw = backend.cat_file_batch(repo_dir, sha, paths)
        return {path: raw[path].decode("utf-8") for path in paths}
    except GitError:
        out: dict[str, str] = {}
        for path in paths:
            try:
                out[path] = backend.cat_file(repo_dir, sha, path)
            except GitError:
                continue
        return out


def set_backend(backend: GitBackend) -> None:
    """Override the active git backend. Tests can swap in a fake here."""
    global _default_backend
    _default_backend = backend


def reset_backend() -> None:
    """Restore the active git backend to a fresh `RealGitBackend`."""
    global _default_backend
    _default_backend = RealGitBackend()
