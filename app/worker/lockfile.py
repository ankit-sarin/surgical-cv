"""Single-worker file lock — refuse to start a second instance.

Non-blocking fcntl.LOCK_EX on a sentinel file. Daemon mode holds the lock
for the process lifetime; --once acquires + releases per invocation.

The lock file is the convention point — its path lives under ``or-raw/``
so it shares the manifest's NAS dirfd / mount semantics. Default:
``<nas_root>/or-raw/.worker.lock``. Tests override the path directly."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkerAlreadyRunningError(RuntimeError):
    """Another worker process holds the lock."""


@contextmanager
def single_worker_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive non-blocking flock on ``lock_path``. Yields if
    acquired; raises ``WorkerAlreadyRunningError`` if another process already
    holds the lock. Releases on context exit (or process death)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise WorkerAlreadyRunningError(
                f"another worker holds {lock_path}"
            ) from e
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def default_lock_path(nas_root: Path) -> Path:
    return nas_root / "or-raw" / ".worker.lock"
