"""Raw-segment repository — surgeon's view of unclaimed BDV segments.

v1 backing: ``os.scandir`` against ``<nas_root>/raw-{folder_slug}/``. The
NAS root is resolved by ``pipeline.paths.nas_root`` (env var
``PIPELINE_NAS_ROOT``, default ``/mnt/nas``) — F-012 collapsed the prior
``RAW_VIDEO_ROOT`` env var into ``PIPELINE_NAS_ROOT`` so a single env-var
change moves both the marker writer and the worker scanner together.
Stateless reads. Tests use the in-memory fake rather than
tmpdir-with-touch'd-files when they just need scope behavior.

The BDV recorder names segments ``capt0_YYYYMMDD-HHMMSS.mp4``. Once the
pipeline claims a segment (Pass 1 concat), it's renamed to
``capt0_YYYYMMDD-HHMMSS-copied.mp4``. Anything with a suffix between the
timestamp and ``.mp4`` is therefore *already claimed* or *in-flight* — we
exclude them from the surgeon's intake view.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pipeline.bdv import BDV_UNCLAIMED_RE  # F-015: shared canonical pattern
from pipeline.paths import nas_root as _nas_root


def raw_root() -> Path:
    """Thin shim around ``pipeline.paths.nas_root`` retained for caller
    compatibility (``app/repos/cases.py`` imports it). The body intentionally
    holds no env-var read of its own — F-012 enforces the single-source rule."""
    return _nas_root()


@dataclass(frozen=True)
class SegmentRecord:
    filename: str
    timestamp: datetime
    size_bytes: int
    path: Path


class RawSegmentRepository(Protocol):
    def list_raw_segments(self, folder_slug: str) -> list[SegmentRecord]: ...


def _parse_bdv_timestamp(filename: str) -> datetime | None:
    m = BDV_UNCLAIMED_RE.match(filename)
    if not m:
        return None
    date_str, time_str = m.groups()
    try:
        return datetime.strptime(
            f"{date_str}{time_str}", "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class FilesystemRawSegmentRepository:
    """Reads ``raw-<folder_slug>/`` under ``raw_root()`` fresh on every call.

    Missing folder → empty list (treat as "no segments yet", not exception —
    same UX as having an empty folder). Non-matching files silently skipped.
    """

    def __init__(self, root: Path | None = None):
        self._root_override = root

    def _root(self) -> Path:
        return self._root_override or raw_root()

    def list_raw_segments(self, folder_slug: str) -> list[SegmentRecord]:
        folder = self._root() / f"raw-{folder_slug}"
        if not folder.is_dir():
            return []
        records: list[SegmentRecord] = []
        with os.scandir(folder) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                ts = _parse_bdv_timestamp(entry.name)
                if ts is None:
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                records.append(
                    SegmentRecord(
                        filename=entry.name,
                        timestamp=ts,
                        size_bytes=size,
                        path=Path(entry.path),
                    )
                )
        return records


class InMemoryRawSegmentRepository:
    """Test fake. ``segments_by_folder`` maps folder_slug → list of records."""

    def __init__(
        self,
        segments_by_folder: dict[str, list[SegmentRecord]] | None = None,
    ):
        self._data = {
            k: list(v) for k, v in (segments_by_folder or {}).items()
        }

    def list_raw_segments(self, folder_slug: str) -> list[SegmentRecord]:
        return list(self._data.get(folder_slug, []))
