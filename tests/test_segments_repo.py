"""Tests for ``app/repos/segments.py`` — FilesystemRawSegmentRepository
against tmpdir BDV files, plus the in-memory fake."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.repos.segments import (
    FilesystemRawSegmentRepository,
    InMemoryRawSegmentRepository,
    SegmentRecord,
    raw_root,
)


# ----- raw_root() env override -----


def test_raw_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_NAS_ROOT", str(tmp_path))
    assert raw_root() == tmp_path


def test_raw_root_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_NAS_ROOT", raising=False)
    assert str(raw_root()) == "/mnt/nas"


# ----- FilesystemRawSegmentRepository: discovery -----


def _make_segment(folder: Path, name: str, size: int = 1_000_000) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"\x00" * size)
    return p


def test_filesystem_missing_folder_returns_empty(tmp_path):
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    assert repo.list_raw_segments("sarin") == []


def test_filesystem_empty_folder_returns_empty(tmp_path):
    (tmp_path / "raw-sarin").mkdir()
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    assert repo.list_raw_segments("sarin") == []


def test_filesystem_picks_up_canonical_bdv_files(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082000.mp4")
    _make_segment(folder, "capt0_20260102-083000.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    result = repo.list_raw_segments("sarin")
    assert {s.filename for s in result} == {
        "capt0_20260102-082000.mp4",
        "capt0_20260102-083000.mp4",
    }


def test_filesystem_skips_copied_suffix(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082000.mp4")
    _make_segment(folder, "capt0_20260102-083000-copied.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    result = repo.list_raw_segments("sarin")
    assert {s.filename for s in result} == {"capt0_20260102-082000.mp4"}


def test_filesystem_skips_pending_suffix(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082000.mp4")
    _make_segment(folder, "capt0_20260102-083000-pending.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    result = repo.list_raw_segments("sarin")
    assert {s.filename for s in result} == {"capt0_20260102-082000.mp4"}


def test_filesystem_skips_non_mp4_extensions(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082000.mp4")
    _make_segment(folder, "capt0_20260102-083000.mov")
    _make_segment(folder, "capt0_20260102-084000.mkv")
    _make_segment(folder, "README.txt")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    result = repo.list_raw_segments("sarin")
    assert {s.filename for s in result} == {"capt0_20260102-082000.mp4"}


def test_filesystem_skips_subdirectories(tmp_path):
    folder = tmp_path / "raw-sarin"
    folder.mkdir()
    (folder / "subdir").mkdir()
    _make_segment(folder, "capt0_20260102-082000.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    result = repo.list_raw_segments("sarin")
    assert len(result) == 1


# ----- FilesystemRawSegmentRepository: record contents -----


def test_filesystem_parses_timestamp_correctly(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082045.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    [rec] = repo.list_raw_segments("sarin")
    assert rec.timestamp == datetime(2026, 1, 2, 8, 20, 45, tzinfo=timezone.utc)


def test_filesystem_record_includes_size_bytes(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082000.mp4", size=12345)
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    [rec] = repo.list_raw_segments("sarin")
    assert rec.size_bytes == 12345


def test_filesystem_record_includes_full_path(tmp_path):
    folder = tmp_path / "raw-sarin"
    seg = _make_segment(folder, "capt0_20260102-082000.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    [rec] = repo.list_raw_segments("sarin")
    assert rec.path == seg


# ----- FilesystemRawSegmentRepository: folder scoping -----


def test_filesystem_only_reads_requested_folder(tmp_path):
    """Surgeon's folder_slug must scope the listing — sarin doesn't see
    miller's segments even if the same root."""
    _make_segment(tmp_path / "raw-sarin", "capt0_20260102-082000.mp4")
    _make_segment(tmp_path / "raw-miller", "capt0_20260103-100000.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    sarin = repo.list_raw_segments("sarin")
    miller = repo.list_raw_segments("miller")
    assert {s.filename for s in sarin} == {"capt0_20260102-082000.mp4"}
    assert {s.filename for s in miller} == {"capt0_20260103-100000.mp4"}


# ----- FilesystemRawSegmentRepository: stateless re-read -----


def test_filesystem_reads_fresh_each_call(tmp_path):
    folder = tmp_path / "raw-sarin"
    _make_segment(folder, "capt0_20260102-082000.mp4")
    repo = FilesystemRawSegmentRepository(root=tmp_path)
    assert len(repo.list_raw_segments("sarin")) == 1

    _make_segment(folder, "capt0_20260102-083000.mp4")
    assert len(repo.list_raw_segments("sarin")) == 2


# ----- FilesystemRawSegmentRepository: env-resolved root -----


def test_filesystem_root_from_env(monkeypatch, tmp_path):
    _make_segment(tmp_path / "raw-sarin", "capt0_20260102-082000.mp4")
    monkeypatch.setenv("PIPELINE_NAS_ROOT", str(tmp_path))
    repo = FilesystemRawSegmentRepository()  # no explicit root
    assert len(repo.list_raw_segments("sarin")) == 1


# ----- InMemoryRawSegmentRepository -----


def _rec(name: str, hour: int) -> SegmentRecord:
    return SegmentRecord(
        filename=name,
        timestamp=datetime(2026, 1, 2, hour, 0, tzinfo=timezone.utc),
        size_bytes=1_000_000,
        path=Path(f"/tmp/raw-x/{name}"),
    )


def test_inmem_returns_segments_for_folder():
    repo = InMemoryRawSegmentRepository({
        "sarin": [_rec("capt0_20260102-080000.mp4", 8)],
        "miller": [_rec("capt0_20260102-090000.mp4", 9)],
    })
    sarin = repo.list_raw_segments("sarin")
    assert [s.filename for s in sarin] == ["capt0_20260102-080000.mp4"]


def test_inmem_unknown_folder_returns_empty():
    repo = InMemoryRawSegmentRepository()
    assert repo.list_raw_segments("ghost") == []


def test_inmem_returns_copy_not_internal_list():
    rec = _rec("capt0_20260102-080000.mp4", 8)
    repo = InMemoryRawSegmentRepository({"sarin": [rec]})
    result = repo.list_raw_segments("sarin")
    result.clear()
    # Internal state must not be affected.
    assert len(repo.list_raw_segments("sarin")) == 1
