"""Marker discovery, parsing, and surgeon-folder cross-validation.

Markers are ``.ready-UCD-FIL-###.json`` files in ``<nas_root>/raw-<surgeon>/``,
written by ``CaseRepository.submit_case``. The worker scans every surgeon's
raw folder and yields markers in FIFO order (oldest mtime first) to avoid
starvation under load.

Quarantine subdirs the worker uses per-folder:
  ``.processed/``  — successful dispatch (or verify soft-fail terminal).
  ``.failed/``     — hard failure (pipeline returncode≠0, missing manifest row).
  ``.malformed/``  — file present but JSON malformed / shape wrong / surgeon mismatch.

The worker creates these on demand. Operators can move a ``.failed/`` marker
back to the parent folder to re-trigger; the worker has no automated retry
in v1."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_RAW_DIR_RE = re.compile(r"^raw-([a-z][a-z0-9-]*)$")
_MARKER_RE = re.compile(r"^\.ready-(UCD-FIL-\d{3,})\.json$")


@dataclass(frozen=True)
class Marker:
    """Parsed marker — what dispatch needs to drive the pipeline."""
    path: Path
    ucd_fil_id: str
    surgeon: str
    submitted_at: str
    segments: list[str]


@dataclass(frozen=True)
class MalformedMarker:
    """A marker file that exists but failed parse / validation. Carries the
    reason so the worker can log + quarantine without raising."""
    path: Path
    reason: str


def _list_raw_dirs(nas_root: Path) -> list[Path]:
    """Return every ``raw-<surgeon>/`` directly under ``nas_root``."""
    if not nas_root.is_dir():
        return []
    out: list[Path] = []
    for entry in nas_root.iterdir():
        if entry.is_dir() and _RAW_DIR_RE.match(entry.name):
            out.append(entry)
    return out


def _list_marker_paths(raw_dir: Path) -> list[Path]:
    """Return every direct child file matching the canonical marker pattern.
    Quarantine subdirs are skipped (they're not iterated)."""
    out: list[Path] = []
    for entry in raw_dir.iterdir():
        if entry.is_file() and _MARKER_RE.match(entry.name):
            out.append(entry)
    return out


def parse_marker(path: Path) -> Marker | MalformedMarker:
    """Parse a marker file. Returns a :class:`Marker` on success or a
    :class:`MalformedMarker` with the reason on any failure. Never raises."""
    expected_id_match = _MARKER_RE.match(path.name)
    if not expected_id_match:
        return MalformedMarker(path, f"filename does not match marker pattern")
    expected_id = expected_id_match.group(1)

    try:
        raw = path.read_text()
    except OSError as e:
        return MalformedMarker(path, f"could not read marker: {e}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return MalformedMarker(path, f"JSON parse error: {e}")

    if not isinstance(payload, dict):
        return MalformedMarker(path, "JSON root is not an object")

    for field in ("ucd_fil_id", "surgeon", "submitted_at", "segments"):
        if field not in payload:
            return MalformedMarker(path, f"missing required field {field!r}")

    if payload["ucd_fil_id"] != expected_id:
        return MalformedMarker(
            path,
            f"filename ucd_fil_id {expected_id!r} does not match payload "
            f"ucd_fil_id {payload['ucd_fil_id']!r}",
        )

    raw_dir_match = _RAW_DIR_RE.match(path.parent.name)
    if not raw_dir_match:
        return MalformedMarker(
            path,
            f"marker parent {path.parent.name!r} is not a raw-<surgeon> dir",
        )
    folder_surgeon = raw_dir_match.group(1)
    if payload["surgeon"] != folder_surgeon:
        return MalformedMarker(
            path,
            f"marker surgeon {payload['surgeon']!r} does not match "
            f"folder surgeon {folder_surgeon!r} — possible cross-folder claim",
        )

    segments = payload["segments"]
    if not isinstance(segments, list) or not all(
        isinstance(s, str) and s for s in segments
    ):
        return MalformedMarker(
            path, "segments must be a list of non-empty strings"
        )

    submitted_at = payload["submitted_at"]
    if not isinstance(submitted_at, str) or not submitted_at:
        return MalformedMarker(path, "submitted_at must be a non-empty string")

    return Marker(
        path=path,
        ucd_fil_id=payload["ucd_fil_id"],
        surgeon=payload["surgeon"],
        submitted_at=submitted_at,
        segments=segments,
    )


def scan_markers(nas_root: Path) -> Iterator[Path]:
    """Yield marker paths across every surgeon folder, oldest mtime first
    (FIFO so no surgeon's submissions starve under sustained load)."""
    paths: list[Path] = []
    for raw_dir in _list_raw_dirs(nas_root):
        paths.extend(_list_marker_paths(raw_dir))
    paths.sort(key=lambda p: p.stat().st_mtime)
    yield from paths
