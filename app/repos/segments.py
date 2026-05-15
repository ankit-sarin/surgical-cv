"""Raw-segment repository — surgeon's view of unclaimed BDV segments.

v1 backing: ``os.scandir`` against ``/mnt/nas/raw-{folder_slug}/`` (override
the root with ``RAW_VIDEO_ROOT``, same convention as ``APP_DB_PATH`` /
``PIPELINE_PICKLIST_DIR``). Stateless reads. Tests use the in-memory fake
rather than tmpdir-with-touch'd-files when they just need scope behavior.

The BDV recorder names segments ``capt0_YYYYMMDD-HHMMSS.mp4``. Once the
pipeline claims a segment (Pass 1 concat), it's renamed to
``capt0_YYYYMMDD-HHMMSS-copied.mp4``. Anything with a suffix between the
timestamp and ``.mp4`` is therefore *already claimed* or *in-flight* — we
exclude them from the surgeon's intake view.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


_DEFAULT_RAW_ROOT = Path("/mnt/nas")

# Canonical, *unclaimed* form only — no suffix between timestamp and .mp4.
_BDV_CANONICAL_RE = re.compile(r"^capt0_(\d{8})-(\d{6})\.mp4$")


def raw_root() -> Path:
    env = os.environ.get("RAW_VIDEO_ROOT")
    if env:
        return Path(env)
    return _DEFAULT_RAW_ROOT


@dataclass(frozen=True)
class SegmentRecord:
    filename: str
    timestamp: datetime
    size_bytes: int
    path: Path


class RawSegmentRepository(Protocol):
    def list_raw_segments(self, folder_slug: str) -> list[SegmentRecord]: ...


def _parse_bdv_timestamp(filename: str) -> datetime | None:
    m = _BDV_CANONICAL_RE.match(filename)
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
