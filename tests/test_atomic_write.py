"""Tests for ``pipeline.atomic_write.write_atomic`` — the F-014
consolidation primitive. Three production callers (CsvTable._commit,
_write_ready_marker, scripts/migrate_manifest_spec_j._atomic_write) all
delegate here; their existing test coverage exercises the happy path
through this helper. The test below covers the exception-cleanup invariant
explicitly so a regression in the cleanup branch can't slip through."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.atomic_write import write_atomic


def test_write_atomic_happy_path(tmp_path):
    """Baseline: writer fills the file, write_atomic renames it into place."""
    target = tmp_path / "out.txt"

    def writer(f):
        f.write("hello")

    write_atomic(target, writer)

    assert target.read_text() == "hello"
    # No leftover tempfile in the destination directory.
    siblings = [p.name for p in tmp_path.iterdir()]
    assert siblings == ["out.txt"]


def test_write_atomic_creates_parent_directory(tmp_path):
    """Callers shouldn't have to mkdir before invoking write_atomic — the
    helper creates missing parents."""
    target = tmp_path / "nested" / "deeper" / "out.txt"
    assert not target.parent.exists()

    write_atomic(target, lambda f: f.write("x"))

    assert target.read_text() == "x"


def test_write_atomic_unlinks_tempfile_on_writer_failure(tmp_path):
    """F-014 invariant: when the body_writer raises, the partial tempfile
    is unlinked. Otherwise repeated failures leave the destination
    directory accumulating ``.tmp`` orphans."""
    target = tmp_path / "out.txt"

    class _Boom(RuntimeError):
        pass

    def writer(f):
        f.write("partial")
        raise _Boom("simulated mid-write failure")

    with pytest.raises(_Boom):
        write_atomic(target, writer)

    # Destination must NOT exist (the rename never ran).
    assert not target.exists()
    # And no orphan tempfile lingers in the parent directory.
    leftovers = list(tmp_path.iterdir())
    assert leftovers == [], (
        f"expected zero leftover files after writer failure; got {leftovers}"
    )


def test_write_atomic_does_not_overwrite_on_writer_failure(tmp_path):
    """Composite invariant: a pre-existing destination must survive a
    failed write_atomic call. The atomic-rename only runs after the writer
    succeeds, so a mid-write failure cannot corrupt the existing file."""
    target = tmp_path / "out.txt"
    target.write_text("original")

    def writer(f):
        f.write("new content")
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError):
        write_atomic(target, writer)

    assert target.read_text() == "original"
