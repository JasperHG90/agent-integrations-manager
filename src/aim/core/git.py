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
    pass


# Disable interactive credential prompts so a misconfigured remote can't hang
# the CLI/TUI indefinitely. Also set a generous hard ceiling on all git ops.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false"}
_GIT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class TagInfo:
    name: str
    sha: str


class GitBackend(Protocol):
    def clone_bare(self, url: str, dest: Path) -> None: ...
    def fetch(self, repo_dir: Path) -> None: ...
    def resolve_ref(self, repo_dir: Path, ref: str) -> str: ...
    def list_tags(self, repo_dir: Path) -> list[TagInfo]: ...
    def latest_tag(self, repo_dir: Path, ref: str) -> str | None: ...
    def ls_tree(self, repo_dir: Path, sha: str, path: str = "") -> list[str]: ...
    def cat_file(self, repo_dir: Path, sha: str, path: str) -> str: ...
    def cat_file_bytes(self, repo_dir: Path, sha: str, path: str) -> bytes: ...
    def cat_file_batch(self, repo_dir: Path, sha: str, paths: list[str]) -> dict[str, bytes]: ...
    def archive(self, repo_dir: Path, sha: str, source_path: str, dest_dir: Path) -> None: ...
    def last_touching_sha(self, repo_dir: Path, ref: str, source_path: str) -> str: ...


def _run(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout: int | None = None,
) -> bytes:
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
        _run(["git", "-C", str(repo_dir), "fetch", "--quiet", "--tags", "--prune", "origin"])

    def resolve_ref(self, repo_dir: Path, ref: str) -> str:
        if ref.startswith("-"):
            raise GitError(f"ref must not start with '-': {ref!r}")
        out = _run(["git", "-C", str(repo_dir), "rev-parse", ref])
        return out.decode().strip()

    def list_tags(self, repo_dir: Path) -> list[TagInfo]:
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
        args = ["git", "-C", str(repo_dir), "ls-tree", "-r", "--name-only", sha]
        if path:
            args += ["--", path]
        out = _run(args)
        return [line for line in out.decode().splitlines() if line]

    def cat_file(self, repo_dir: Path, sha: str, path: str) -> str:
        out = _run(["git", "-C", str(repo_dir), "show", f"{sha}:{path}"])
        return out.decode()

    def cat_file_bytes(self, repo_dir: Path, sha: str, path: str) -> bytes:
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
        """Extract `source_path` subtree at `sha` into `dest_dir`, flattened so
        the contents of source_path/ land directly under dest_dir.

        Implementation: run `git archive` to bytes first (catching git failures
        cleanly), then feed those bytes to `tar -x`. Avoids a pipe deadlock and
        ensures git's stderr surfaces instead of being shadowed by tar's
        "empty input" error.
        """
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


def remove_clone(repo_dir: Path) -> None:
    if repo_dir.exists():
        shutil.rmtree(repo_dir)


_default_backend: GitBackend = RealGitBackend()


def get_backend() -> GitBackend:
    return _default_backend


def set_backend(backend: GitBackend) -> None:
    """Override the active git backend. Tests can swap in a fake here."""
    global _default_backend
    _default_backend = backend


def reset_backend() -> None:
    global _default_backend
    _default_backend = RealGitBackend()
