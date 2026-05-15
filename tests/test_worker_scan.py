"""Tests for ``app/worker/scan.py`` — marker discovery, FIFO ordering,
and parse-time validation (canonical vs malformed / cross-folder claims)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.worker.scan import (
    MalformedMarker,
    Marker,
    parse_marker,
    scan_markers,
)


def _valid_payload(**overrides) -> dict:
    base = {
        "ucd_fil_id": "UCD-FIL-005",
        "surgeon": "sarin",
        "submitted_at": "2026-05-15T08:00:00+00:00",
        "segments": ["capt0_20260515-080000.mp4"],
    }
    base.update(overrides)
    return base


def _drop_marker(
    nas_root: Path, surgeon: str, ucd_fil_id: str, payload: dict | str
) -> Path:
    raw = nas_root / f"raw-{surgeon}"
    raw.mkdir(parents=True, exist_ok=True)
    marker = raw / f".ready-{ucd_fil_id}.json"
    if isinstance(payload, str):
        marker.write_text(payload)
    else:
        marker.write_text(json.dumps(payload))
    return marker


# ----- parse_marker happy path -----


def test_parse_canonical_marker(tmp_path):
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", _valid_payload())
    result = parse_marker(path)
    assert isinstance(result, Marker)
    assert result.ucd_fil_id == "UCD-FIL-005"
    assert result.surgeon == "sarin"
    assert result.segments == ["capt0_20260515-080000.mp4"]


def test_parse_preserves_submitted_at(tmp_path):
    payload = _valid_payload(submitted_at="2026-05-15T12:34:56.789012+00:00")
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, Marker)
    assert result.submitted_at == "2026-05-15T12:34:56.789012+00:00"


def test_parse_preserves_multiple_segments(tmp_path):
    payload = _valid_payload(segments=["a.mp4", "b.mp4", "c.mp4"])
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, Marker)
    assert result.segments == ["a.mp4", "b.mp4", "c.mp4"]


# ----- parse_marker malformed inputs -----


def test_parse_filename_pattern_mismatch(tmp_path):
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    bad = raw / "not-a-marker.json"
    bad.write_text("{}")
    result = parse_marker(bad)
    assert isinstance(result, MalformedMarker)
    assert "pattern" in result.reason


def test_parse_invalid_json(tmp_path):
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", "not_json{{{")
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "JSON" in result.reason


def test_parse_non_object_root(tmp_path):
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", "[1, 2, 3]")
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "object" in result.reason


def test_parse_missing_required_fields(tmp_path):
    payload = _valid_payload()
    del payload["surgeon"]
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "surgeon" in result.reason


def test_parse_filename_id_payload_id_mismatch(tmp_path):
    """Filename says UCD-FIL-005 but payload says UCD-FIL-099 — possible
    bug / tampering. Reject."""
    payload = _valid_payload(ucd_fil_id="UCD-FIL-099")
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "match payload" in result.reason


def test_parse_surgeon_folder_mismatch(tmp_path):
    """Marker in raw-sarin/ but payload claims surgeon=miller — defense
    in depth, refuse to process (could be a cross-folder symlink attack
    or a misfiled copy)."""
    payload = _valid_payload(surgeon="miller")
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "cross-folder" in result.reason


def test_parse_segments_must_be_list(tmp_path):
    payload = _valid_payload(segments="not-a-list")
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "segments" in result.reason


def test_parse_segments_must_be_strings(tmp_path):
    payload = _valid_payload(segments=["good.mp4", 123])
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)
    assert "segments" in result.reason


def test_parse_segments_empty_strings_rejected(tmp_path):
    payload = _valid_payload(segments=["good.mp4", ""])
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)


def test_parse_submitted_at_must_be_non_empty(tmp_path):
    payload = _valid_payload(submitted_at="")
    path = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", payload)
    result = parse_marker(path)
    assert isinstance(result, MalformedMarker)


# ----- scan_markers discovery -----


def test_scan_empty_nas_yields_nothing(tmp_path):
    assert list(scan_markers(tmp_path)) == []


def test_scan_missing_nas_root_yields_nothing(tmp_path):
    """Hardened against the no-NAS-mounted case."""
    assert list(scan_markers(tmp_path / "does-not-exist")) == []


def test_scan_finds_marker_in_surgeon_folder(tmp_path):
    p = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", _valid_payload())
    found = list(scan_markers(tmp_path))
    assert found == [p]


def test_scan_finds_markers_across_surgeons(tmp_path):
    p1 = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", _valid_payload())
    p2 = _drop_marker(
        tmp_path, "miller", "UCD-FIL-099",
        _valid_payload(surgeon="miller", ucd_fil_id="UCD-FIL-099"),
    )
    found = set(scan_markers(tmp_path))
    assert found == {p1, p2}


def test_scan_orders_fifo_oldest_first(tmp_path):
    """FIFO by mtime — older markers process first so heavy submissions
    don't starve light ones."""
    older = _drop_marker(tmp_path, "sarin", "UCD-FIL-005", _valid_payload())
    # Force older mtime by 5 seconds.
    old_t = time.time() - 5
    os.utime(older, (old_t, old_t))
    newer = _drop_marker(
        tmp_path, "sarin", "UCD-FIL-006",
        _valid_payload(ucd_fil_id="UCD-FIL-006"),
    )
    found = list(scan_markers(tmp_path))
    assert found == [older, newer]


def test_scan_skips_quarantine_subdirs(tmp_path):
    """Markers in .processed/ / .failed/ / .malformed/ must not be picked
    up — those are terminal archive locations."""
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    (raw / ".processed").mkdir()
    (raw / ".processed" / ".ready-UCD-FIL-001.json").write_text("{}")
    (raw / ".failed").mkdir()
    (raw / ".failed" / ".ready-UCD-FIL-002.json").write_text("{}")
    (raw / ".malformed").mkdir()
    (raw / ".malformed" / ".ready-UCD-FIL-003.json").write_text("{}")
    # Plus one fresh marker in the parent.
    fresh = raw / ".ready-UCD-FIL-005.json"
    fresh.write_text(json.dumps(_valid_payload()))
    assert list(scan_markers(tmp_path)) == [fresh]


def test_scan_ignores_non_raw_dirs(tmp_path):
    """``deid-sarin/`` and ``or-raw/`` are siblings to raw-*; never scan
    those (only ``raw-<surgeon>/`` matches the pattern)."""
    (tmp_path / "deid-sarin").mkdir()
    (tmp_path / "deid-sarin" / ".ready-UCD-FIL-005.json").write_text("{}")
    (tmp_path / "or-raw").mkdir()
    (tmp_path / "or-raw" / ".ready-UCD-FIL-006.json").write_text("{}")
    assert list(scan_markers(tmp_path)) == []


def test_scan_ignores_non_marker_files_in_raw(tmp_path):
    """Real BDV segments live next to markers in raw-<surgeon>/. They must
    not appear in the marker scan."""
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    (raw / "capt0_20260515-080000.mp4").write_bytes(b"")
    (raw / "README.txt").write_text("notes")
    marker = raw / ".ready-UCD-FIL-005.json"
    marker.write_text(json.dumps(_valid_payload()))
    assert list(scan_markers(tmp_path)) == [marker]
