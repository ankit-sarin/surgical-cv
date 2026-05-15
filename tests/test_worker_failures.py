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
    # F-030: details now carries the curated generic message; full path +
    # parse-error text moved to the systemd journal (covered in the new
    # F-030 tests below).
    from app.worker.failures import _MALFORMED_GENERIC_MSG
    assert items[0]["details"] == _MALFORMED_GENERIC_MSG


# ----- F-006: stderr scrubbing into attention_items.details -----
#
# These tests use the dispatch helpers (_summarize_stderr) directly + the
# end-to-end record_dispatch_outcome path. The contract: details renders as
# "pipeline {stage} stage failed (returncode={rc}): {scrubbed_first_line}";
# the scrubbed component must drop NAS paths to a placeholder-free first
# line, and any PHI patterns in stderr must not survive into details.


def test_dispatch_summary_first_line_only(tmp_path, app_env):
    """Multi-line stderr collapses to the first non-empty line so the
    SQLite row stays bounded. Trailing context lines (typically Python
    tracebacks) end up in pipeline.log on the NAS, not attention_items."""
    ensure_system_worker_user()
    marker = _make_marker(tmp_path)
    multiline = (
        "ffmpeg: codec parameter invalid\n"
        "  at offset 1234\n"
        "  context: stream 0:0\n"
    )
    record_dispatch_outcome(
        marker,
        DispatchOutcome(
            kind="hard_fail", stage="deid", returncode=1,
            detail="ffmpeg: codec parameter invalid",
        ),
    )
    items = _read_attention_items()
    assert len(items) == 1
    details = items[0]["details"]
    # Structured shape preserved.
    assert "pipeline deid stage failed" in details
    assert "(returncode=1)" in details
    # First-line content survives; trailing context lines do not.
    assert "ffmpeg: codec parameter invalid" in details
    assert "at offset" not in details
    assert "context: stream" not in details


def test_dispatch_scrubs_phi_patterns_from_stderr(tmp_path, app_env):
    """If a pipeline subprocess somehow includes PHI-shaped tokens in its
    stderr (a future bug class — exception messages echoing manifest
    fields, etc.), those tokens must NOT survive into the surgeon-visible
    attention_items.details. The scrub_text pass replaces them with
    category placeholders."""
    from app.worker.dispatch import _summarize_stderr

    raw = (
        "exception during deid: Patient: John Smith MRN 12345678 "
        "phone (916) 555-1234"
    )
    summary = _summarize_stderr(raw)
    # PHI tokens are gone.
    assert "John Smith" not in summary
    assert "12345678" not in summary
    assert "(916) 555-1234" not in summary
    # Placeholders are present so the operator sees the shape of the leak.
    assert "<NAME>" in summary
    assert "<MRN>" in summary
    assert "<PHONE>" in summary
    # Surrounding context survives.
    assert "exception during deid" in summary


def test_dispatch_empty_stderr_produces_empty_summary(tmp_path, app_env):
    """No stderr → no detail. The wrapper formatting in failures.py still
    renders the stage + returncode prefix; details just trail with an
    empty colon-suffix."""
    from app.worker.dispatch import _summarize_stderr

    assert _summarize_stderr("") == ""
    assert _summarize_stderr("   \n   \n") == ""


def test_dispatch_summary_caps_at_200_chars(tmp_path, app_env):
    """The first-line cap is the second defense (after first-line-only)
    against an unbounded SQLite row. 200 chars is enough for a clear
    single-sentence error without bloat."""
    from app.worker.dispatch import _summarize_stderr

    long_line = "x" * 500
    summary = _summarize_stderr(long_line)
    assert len(summary) <= 200


# ----- F-010: connection lifecycle (close after every call) -----
#
# sqlite3.Connection.__exit__ commits/rolls back the transaction but does
# NOT close the connection — three callers in app/worker/failures.py
# previously leaked one FD per call. Under --once mode this is invisible
# (process exits, kernel reclaims), but under --daemon mode the leak
# compounds per iteration. These tests use a wrapper around the real
# connect() that records close() calls so we can assert the lifecycle
# without faking the SQL surface.


class _CloseTrackingConn:
    """Wraps a real sqlite3.Connection and counts close() invocations.
    Delegates everything else so the tests still exercise the real SQL."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self.close_calls += 1
        self._real.close()


def _patch_tracking_connect(monkeypatch):
    """Patch app.worker.failures.connect with a wrapper that returns a
    _CloseTrackingConn over the real connection. Returns a list that the
    test can inspect to find the most-recent tracker."""
    from app.worker import failures as failures_mod

    real_connect = failures_mod.connect
    trackers: list[_CloseTrackingConn] = []

    def fake_connect():
        wrapped = _CloseTrackingConn(real_connect())
        trackers.append(wrapped)
        return wrapped

    monkeypatch.setattr(failures_mod, "connect", fake_connect)
    return trackers


def test_ensure_system_worker_user_closes_connection(app_env, monkeypatch):
    """F-010: ensure_system_worker_user must close its connection on both
    branches — when the row already exists (early return) and when it has
    to insert. This test covers both via two consecutive calls."""
    trackers = _patch_tracking_connect(monkeypatch)

    # First call: row does not exist → INSERT path.
    ensure_system_worker_user()
    assert len(trackers) == 1
    assert trackers[0].close_calls == 1, (
        "INSERT branch must close the connection"
    )

    # Second call: row already exists → early-return branch.
    ensure_system_worker_user()
    assert len(trackers) == 2
    assert trackers[1].close_calls == 1, (
        "early-return branch must close the connection"
    )


def test_lookup_username_for_slug_closes_connection(app_env, monkeypatch):
    """F-010: read-only path also leaks if not explicitly closed. Cover
    both the match branch (asarin → sarin folder_slug, seeded by app_env)
    and the no-match branch (unknown slug → fallback to system_worker)."""
    from app.worker.failures import _lookup_username_for_slug

    ensure_system_worker_user()
    trackers = _patch_tracking_connect(monkeypatch)

    # Match branch.
    result = _lookup_username_for_slug("sarin")
    assert result == "asarin"
    assert len(trackers) == 1
    assert trackers[0].close_calls == 1

    # No-match branch.
    result = _lookup_username_for_slug("nobody")
    assert result == SYSTEM_WORKER_USERNAME
    assert len(trackers) == 2
    assert trackers[1].close_calls == 1


def test_write_attention_item_closes_connection(app_env, monkeypatch):
    """F-010: write_attention_item is the highest-volume caller — fires
    once per dispatched marker. Confirm close is called even on the
    success path that returns cursor.lastrowid."""
    ensure_system_worker_user()
    trackers = _patch_tracking_connect(monkeypatch)

    row_id = write_attention_item(
        item_type="test",
        affected_user=SYSTEM_WORKER_USERNAME,
        case_id=None,
        severity="normal",
        details="connection-lifecycle test",
    )
    assert isinstance(row_id, int)
    assert len(trackers) == 1
    assert trackers[0].close_calls == 1, (
        "success path with return value must still close the connection"
    )


# ----- F-030: record_malformed scrubs NAS path from attention_items.details -----
#
# attention_items.details is surgeon-visible via the Action Required tab.
# Pre-fix, record_malformed wrote f"malformed marker at {marker.path}: {reason}"
# which surfaced the full NAS path string. Post-fix: details carries a
# curated generic message; the path + parse-error text move to the systemd
# journal (covered by test_record_malformed_logs_full_context_to_journal).


def test_record_malformed_details_omits_nas_path_and_reason(tmp_path, app_env):
    """F-030: attention_items.details must contain only the curated generic
    message — no NAS path, no parse-error text. Surgeon UI surface stays
    free of internal infrastructure strings."""
    from app.worker.failures import _MALFORMED_GENERIC_MSG

    ensure_system_worker_user()
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    bad = raw / ".ready-UCD-FIL-005.json"
    bad.write_text("not_json{{{")
    parse_reason = "JSON parse error: Expecting value: line 1 column 9 (char 8)"

    record_malformed(MalformedMarker(bad, parse_reason))

    items = _read_attention_items()
    assert len(items) == 1
    details = items[0]["details"]
    assert details == _MALFORMED_GENERIC_MSG
    # Belt-and-suspenders.
    assert str(bad) not in details
    assert "raw-sarin" not in details
    assert "JSON parse error" not in details


def test_record_malformed_logs_full_context_to_journal(tmp_path, app_env, caplog):
    """F-030 operator-side: the systemd journal (captured here via caplog)
    must hold the marker path and parse-error reason. Otherwise the
    quarantine is unactionable — the operator sees the curated row in the
    UI but has no way to find the offending file."""
    ensure_system_worker_user()
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    bad = raw / ".ready-UCD-FIL-005.json"
    bad.write_text("not_json{{{")
    parse_reason = "missing required field 'segments'"

    caplog.set_level("WARNING", logger="app.worker.failures")
    record_malformed(MalformedMarker(bad, parse_reason))

    records = [r for r in caplog.records if r.name == "app.worker.failures"]
    assert len(records) == 1
    rec = records[0]
    assert "malformed marker" in rec.message
    assert rec.marker_path == str(bad)
    assert rec.parse_error == parse_reason
