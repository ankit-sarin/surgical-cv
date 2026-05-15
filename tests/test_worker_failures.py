"""Tests for ``app/worker/failures.py`` — attention_items writes + marker
archival. Uses the conftest tmp DB so the FK constraints are real."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db.connection import connect
from app.worker.dispatch import DispatchOutcome
from app.worker.failures import (
    SYSTEM_WORKER_USERNAME,
    TYPE_MALFORMED_MARKER,
    TYPE_ORPHAN_MARKER,
    TYPE_PIPELINE_FAILURE,
    TYPE_VERIFY_SOFT_FAIL,
    archive_marker,
    ensure_system_worker_user,
    record_dispatch_outcome,
    record_malformed,
    write_attention_item,
)
from app.worker.scan import MalformedMarker, Marker


def _make_marker(tmp_path, surgeon="sarin", case_id="UCD-FIL-005") -> Marker:
    raw = tmp_path / f"raw-{surgeon}"
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / f".ready-{case_id}.json"
    path.write_text(json.dumps({"id": case_id}))
    return Marker(
        path=path,
        ucd_fil_id=case_id,
        surgeon=surgeon,
        submitted_at="2026-05-15T08:00:00+00:00",
        segments=["a.mp4"],
    )


def _read_attention_items() -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attention_items ORDER BY id"
            ).fetchall()
        ]


# ----- ensure_system_worker_user -----


def test_ensure_system_worker_user_inserts_when_missing(app_env):
    ensure_system_worker_user()
    with connect() as conn:
        row = conn.execute(
            "SELECT username, role, active FROM users WHERE username = ?",
            (SYSTEM_WORKER_USERNAME,),
        ).fetchone()
    assert row is not None
    assert row["role"] == "admin"
    # Stored inactive so admin-CLI list commands don't surface it as a
    # human operator.
    assert row["active"] == 0


def test_ensure_system_worker_user_idempotent(app_env):
    """Two calls leave one row; safe for repeated worker startup."""
    ensure_system_worker_user()
    ensure_system_worker_user()
    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username = ?",
            (SYSTEM_WORKER_USERNAME,),
        ).fetchone()[0]
    assert count == 1


# ----- archive_marker -----


def test_archive_marker_moves_to_processed(tmp_path):
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    src = raw / ".ready-UCD-FIL-005.json"
    src.write_text("{}")
    dest = archive_marker(src, "success")
    assert dest == raw / ".processed" / ".ready-UCD-FIL-005.json"
    assert dest.exists()
    assert not src.exists()


def test_archive_marker_moves_to_failed(tmp_path):
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    src = raw / ".ready-UCD-FIL-005.json"
    src.write_text("{}")
    dest = archive_marker(src, "fail")
    assert dest.parent.name == ".failed"


def test_archive_marker_moves_to_malformed(tmp_path):
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    src = raw / ".ready-UCD-FIL-005.json"
    src.write_text("garbage")
    dest = archive_marker(src, "malformed")
    assert dest.parent.name == ".malformed"


def test_archive_marker_creates_subdir_if_missing(tmp_path):
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    src = raw / ".ready-UCD-FIL-005.json"
    src.write_text("{}")
    assert not (raw / ".processed").exists()
    archive_marker(src, "success")
    assert (raw / ".processed").is_dir()


def test_archive_marker_overwrites_existing_destination(tmp_path):
    """Operator-driven retry: marker re-appears in parent, re-archives."""
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    (raw / ".failed").mkdir()
    stale = raw / ".failed" / ".ready-UCD-FIL-005.json"
    stale.write_text("stale")
    src = raw / ".ready-UCD-FIL-005.json"
    src.write_text("fresh")
    archive_marker(src, "fail")
    assert (raw / ".failed" / ".ready-UCD-FIL-005.json").read_text() == "fresh"


def test_archive_marker_unknown_kind_raises(tmp_path):
    src = tmp_path / "raw-sarin" / ".ready-UCD-FIL-005.json"
    src.parent.mkdir(parents=True)
    src.write_text("{}")
    with pytest.raises(ValueError, match="unknown archive kind"):
        archive_marker(src, "nonsense")


# ----- write_attention_item -----


def test_write_attention_item_inserts_row(app_env):
    ensure_system_worker_user()
    row_id = write_attention_item(
        item_type="test",
        affected_user="asarin",
        case_id="UCD-FIL-005",
        severity="high",
        details="something went wrong",
    )
    items = _read_attention_items()
    assert len(items) == 1
    assert items[0]["id"] == row_id
    assert items[0]["type"] == "test"
    assert items[0]["affected_user"] == "asarin"
    assert items[0]["case_id"] == "UCD-FIL-005"
    assert items[0]["severity"] == "high"
    assert items[0]["created_by"] == SYSTEM_WORKER_USERNAME
    assert items[0]["status"] == "open"


def test_write_attention_item_accepts_null_case_id(app_env):
    """Malformed markers have no case_id — schema allows NULL."""
    ensure_system_worker_user()
    write_attention_item(
        item_type=TYPE_MALFORMED_MARKER,
        affected_user=SYSTEM_WORKER_USERNAME,
        case_id=None,
        severity="normal",
        details="bad marker",
    )
    items = _read_attention_items()
    assert items[0]["case_id"] is None


# ----- record_dispatch_outcome -----


def test_record_outcome_success_archives_no_attention_item(tmp_path, app_env):
    ensure_system_worker_user()
    marker = _make_marker(tmp_path)
    record_dispatch_outcome(marker, DispatchOutcome(kind="success"))
    assert (marker.path.parent / ".processed" / marker.path.name).exists()
    assert _read_attention_items() == []


def test_record_outcome_soft_fail_writes_normal_severity(tmp_path, app_env):
    ensure_system_worker_user()
    marker = _make_marker(tmp_path)
    record_dispatch_outcome(
        marker,
        DispatchOutcome(kind="soft_fail", stage="verify",
                        detail="audio leak"),
    )
    # Terminal-but-flagged → .processed/, not .failed/.
    assert (marker.path.parent / ".processed" / marker.path.name).exists()
    items = _read_attention_items()
    assert len(items) == 1
    assert items[0]["type"] == TYPE_VERIFY_SOFT_FAIL
    assert items[0]["severity"] == "normal"


def test_record_outcome_orphan_writes_high_severity(tmp_path, app_env):
    ensure_system_worker_user()
    marker = _make_marker(tmp_path)
    record_dispatch_outcome(
        marker,
        DispatchOutcome(kind="orphan", detail="no manifest row"),
    )
    assert (marker.path.parent / ".failed" / marker.path.name).exists()
    items = _read_attention_items()
    assert len(items) == 1
    assert items[0]["type"] == TYPE_ORPHAN_MARKER
    assert items[0]["severity"] == "high"


def test_record_outcome_hard_fail_writes_high_severity(tmp_path, app_env):
    ensure_system_worker_user()
    marker = _make_marker(tmp_path)
    record_dispatch_outcome(
        marker,
        DispatchOutcome(
            kind="hard_fail", stage="deid", returncode=2, detail="boom"
        ),
    )
    assert (marker.path.parent / ".failed" / marker.path.name).exists()
    items = _read_attention_items()
    assert len(items) == 1
    assert items[0]["type"] == TYPE_PIPELINE_FAILURE
    assert items[0]["severity"] == "high"
    assert "deid" in items[0]["details"]


def test_record_outcome_uses_real_surgeon_username(tmp_path, app_env):
    """affected_user is the users.username for the surgeon folder_slug —
    asarin (not the slug "sarin")."""
    ensure_system_worker_user()
    marker = _make_marker(tmp_path, surgeon="sarin")
    record_dispatch_outcome(
        marker,
        DispatchOutcome(kind="hard_fail", stage="concat", returncode=1),
    )
    items = _read_attention_items()
    assert items[0]["affected_user"] == "asarin"


def test_record_outcome_unknown_surgeon_falls_back_to_system(tmp_path, app_env):
    """No user with that folder_slug — fall back to system_worker so the
    FK is satisfied, surgeon info preserved in details."""
    ensure_system_worker_user()
    marker = _make_marker(tmp_path, surgeon="ghost")
    record_dispatch_outcome(
        marker,
        DispatchOutcome(kind="hard_fail", stage="concat", returncode=1),
    )
    items = _read_attention_items()
    assert items[0]["affected_user"] == SYSTEM_WORKER_USERNAME


# ----- record_malformed -----


def test_record_malformed_quarantines_and_logs(tmp_path, app_env):
    ensure_system_worker_user()
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    bad = raw / ".ready-UCD-FIL-005.json"
    bad.write_text("not_json{{{")
    record_malformed(MalformedMarker(bad, "JSON parse error"))
    assert (raw / ".malformed" / bad.name).exists()
    items = _read_attention_items()
    assert len(items) == 1
    assert items[0]["type"] == TYPE_MALFORMED_MARKER
    assert items[0]["case_id"] is None
    assert "JSON parse error" in items[0]["details"]
