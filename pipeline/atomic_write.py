"""Atomic file-write primitive — single source of truth for the
mkstemp + fsync + os.replace + cleanup-on-exception idiom.

F-014 consolidated three near-identical implementations (CsvTable._commit,
_write_ready_marker, scripts/migrate_manifest_spec_j._atomic_write) so a
future hardening (stricter perms, directory fsync for true POSIX-strict
atomicity, etc.) lands once instead of three times. Same-directory tempfile
guarantees same-filesystem ``os.replace``; the writer callback fills the
file with whatever shape the caller needs (CSV rows, JSON payload, raw
bytes), so this primitive stays format-agnostic.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import IO, Callable


def write_atomic(
    path: Path,
    body_writer: Callable[[IO], None],
) -> None:
    """Atomically write to ``path`` via a sibling tempfile + os.replace.

    The ``body_writer`` callback receives a writable file-like object that
    the caller fills with content. ``write_atomic`` handles the lifecycle:
    create the tempfile in the destination's parent directory (so
    os.replace is same-filesystem), invoke the writer, fsync, and atomically
    rename. On any exception inside the writer (or during fsync / rename),
    the tempfile is unlinked and the original exception propagates.

    The destination's parent directory is created if missing — callers
    don't have to mkdir first.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", newline="") as f:
            body_writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
