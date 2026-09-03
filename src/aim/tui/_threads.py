"""Daemon-thread runner for network-bound TUI work.

Textual thread workers run on a non-daemon ThreadPoolExecutor whose threads
are JOINED at interpreter exit (concurrent.futures' atexit hook) — after the
TUI itself has torn down. A worker blocked in an HTTP call therefore stalls
`aim` between "screen gone" and "shell prompt back", even though app shutdown
looked instant from inside Textual.

Network fetches that are cache-warming or user-initiated lookups run here
instead: a daemon thread dies with the process. Callers marshal results back
with `app.call_from_thread` wrapped in a broad except — the app or screen may
already be gone — and guard against stale deliveries themselves (e.g. with a
sequence counter), since there is no worker-cancellation machinery.

Local, fast, or state-mutating work (installs, renders) should stay on real
Textual workers: those are the ones that must NOT be abandoned mid-write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


def run_detached(target: Callable[[], None], *, name: str) -> threading.Thread:
    """Start `target` on a named daemon thread and return the thread."""
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread
