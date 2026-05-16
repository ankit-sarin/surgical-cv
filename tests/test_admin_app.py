"""Integration tests for the Brief #4 admin Gradio app.

Three layers:

  1. Global Dashboard render — empty and populated state.
  2. Cross-silo Action Required filters — type, surgeon, severity, age.
  3. Dismiss + resolve-on-behalf actions — happy path, validation gate,
     audit row shape (actor_role='admin', resolved_on_behalf_of).
  4. Display label rendering for malformed_marker.

The admin app's handlers are called directly with a fake ``gr.Request``
that carries an authenticated admin session cookie — same pattern the
surgeon-app tests use, just with role='admin'.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from app.auth import SESSION_COOKIE_NAME, encode_session
from app.db.connection import connect, utcnow
from app.attention_actions import display_for_type
from app.admin_app import (
    _admin_dismiss_handler,
    _admin_resolve_handler,
    _compute_dashboard,
    _scope_from_request,
    render_ar,
    render_dashboard,
)


_SEED_TS = "2026-05-15T08:00:00+00:00"


# ----- helpers -----


def _admin_request() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: encode_session("ankitsarin")}
    )


def _surgeon_request(username: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: encode_session(username)}
    )


def _ensure_user(db_path, username: str, role: str = "admin") -> None:
    """Upsert a placeholder user row so an INSERT into attention_items
    that references it can satisfy the FK. Used for ``system_worker``
    which is created on demand by the worker but not by the conftest
    seed."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        row = conn.execute(
            "SELECT username FROM users WHERE username = ?", (username,),
        ).fetchone()
        if row is not None:
            return
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, "
            " active, created_at) VALUES (?, ?, NULL, NULL, 0, ?)",
            (username, role, _SEED_TS),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_attention(
    db_path,
    *,
    item_type: str,
    affected_user: str = "asarin",
    case_id: str | None = "UCD-FIL-001",
    severity: str = "normal",
    details: str = "test detail line",
    created_at: str = _SEED_TS,
) -> int:
    if affected_user == "system_worker":
        _ensure_user(db_path, "system_worker", role="admin")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.execute(
            "INSERT INTO attention_items "
            "(type, case_id, affected_user, severity, details, "
            " created_at, created_by, updated_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (
                item_type, case_id, affected_user, severity, details,
                created_at, "asarin", created_at,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _read_audit_rows(db_path) -> list[dict]:
    conn = connect(db_path)
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM admin_audit ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _read_attention_item(db_path, item_id: int) -> dict:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM attention_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ===== Global Dashboard rendering =====


def test_global_dashboard_empty_state_renders(app_env):
    """Zero cases, zero AR items — strip shows zeros, surgeon table
    has one row per seeded surgeon."""
    strip_html, df_update = render_dashboard(_admin_request())
    # Five zero stats.
    assert ">0<" in strip_html  # at least one zero somewhere
    assert "Total cases" in strip_html
    # gr.update result; pull out the value field.
    rows = df_update.get("value") if isinstance(df_update, dict) else df_update.value
    # Two active surgeons in conftest seed: asarin (sarin), anoren (noren).
    assert len(rows) == 2
    # Each row has 6 columns.
    assert all(len(r) == 6 for r in rows)


def test_global_dashboard_populated_state_renders(app_env):
    """Manifest has 3 default cases (2 sarin, 1 miller — no user
    account); seed 2 AR items split across surgeons. The strip totals
    cover both. The per-surgeon table reflects per-username AR counts."""
    _seed_attention(app_env, item_type="phi_redacted",
                    affected_user="asarin", case_id="UCD-FIL-001")
    _seed_attention(app_env, item_type="pipeline_failure",
                    affected_user="anoren", case_id="UCD-FIL-099",
                    severity="high")
    strip_html, df_update = render_dashboard(_admin_request())
    # Total cases (3) + open AR (2) + high-severity (1) appear in
    # the strip's interior.
    assert ">3<" in strip_html or "3</div>" in strip_html
    assert ">2<" in strip_html or "2</div>" in strip_html
    rows = df_update.get("value") if isinstance(df_update, dict) else df_update.value
    # asarin row has 1 open AR; anoren row has 1 (1 high).
    by_label = {r[0]: r for r in rows}
    asarin_row = by_label.get("asarin") or rows[0]
    anoren_row = by_label.get("anoren") or rows[1]
    assert int(asarin_row[4]) == 1  # Open AR
    assert int(anoren_row[4]) == 1
    assert int(anoren_row[5]) == 1  # High-severity


# ===== Cross-silo Action Required filters =====


def test_ar_no_filter_returns_all_open_items(app_env):
    _seed_attention(app_env, item_type="phi_redacted",
                    affected_user="asarin", case_id="UCD-FIL-001")
    _seed_attention(app_env, item_type="pipeline_failure",
                    affected_user="anoren", case_id="UCD-FIL-099")
    df_update, cached_rows, _detail = render_ar(
        _admin_request(),
        type_filter="All types",
        surgeon_filter="All surgeons",
        severity_filter="All",
        age_filter=0,
    )
    assert len(cached_rows) == 2


def test_ar_type_filter_narrows(app_env):
    _seed_attention(app_env, item_type="phi_redacted",
                    affected_user="asarin")
    _seed_attention(app_env, item_type="pipeline_failure",
                    affected_user="anoren", case_id="UCD-FIL-099")
    df_update, cached_rows, _detail = render_ar(
        _admin_request(),
        type_filter="phi_redacted",
        surgeon_filter="All surgeons",
        severity_filter="All",
        age_filter=0,
    )
    assert len(cached_rows) == 1
    assert cached_rows[0]["item_type"] == "phi_redacted"


def test_ar_multi_filter_ands_together(app_env):
    """Type AND surgeon AND severity — only the row matching all three
    survives."""
    _seed_attention(app_env, item_type="pipeline_failure",
                    affected_user="asarin", severity="high",
                    case_id="UCD-FIL-001")
    _seed_attention(app_env, item_type="pipeline_failure",
                    affected_user="anoren", severity="normal",
                    case_id="UCD-FIL-099")
    _seed_attention(app_env, item_type="phi_redacted",
                    affected_user="asarin", severity="normal",
                    case_id="UCD-FIL-002")
    _, cached_rows, _ = render_ar(
        _admin_request(),
        type_filter="pipeline_failure",
        surgeon_filter="sarin",
        severity_filter="high",
        age_filter=0,
    )
    assert len(cached_rows) == 1
    assert cached_rows[0]["item_type"] == "pipeline_failure"
    assert cached_rows[0]["severity"] == "high"


def test_ar_age_filter_cuts_recent_items(app_env):
    """Age slider at 5 → only items older than 5 days survive. Items
    created today get age 0 and drop out."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=10)).isoformat()
    new_ts = now.isoformat()
    _seed_attention(app_env, item_type="phi_redacted",
                    affected_user="asarin",
                    case_id="UCD-FIL-001", created_at=old_ts)
    _seed_attention(app_env, item_type="phi_redacted",
                    affected_user="anoren", case_id="UCD-FIL-099",
                    created_at=new_ts)
    _, cached_rows, _ = render_ar(
        _admin_request(),
        type_filter="All types",
        surgeon_filter="All surgeons",
        severity_filter="All",
        age_filter=5,
    )
    # Only the 10-day-old item survives.
    assert len(cached_rows) == 1
    assert cached_rows[0]["age_days"] >= 5


# ===== Dismiss action =====


def test_admin_dismiss_valid_reason_creates_audit_and_dismisses(app_env):
    item_id = _seed_attention(
        app_env, item_type="malformed_marker",
        affected_user="system_worker", case_id=None,
    )
    df_update, cached_rows, detail_html, item_state, reason_val, error_md = (
        _admin_dismiss_handler(
            _admin_request(), item_id,
            "Unrecognized BDV file from operator station 3",
            "All types", "All surgeons", "All", 0,
        )
    )
    # No inline error.
    assert error_md == ""
    # Item now closed.
    row = _read_attention_item(app_env, item_id)
    assert row["status"] == "dismissed"
    # Audit row written with admin shape.
    audit = _read_audit_rows(app_env)
    assert len(audit) == 1
    assert audit[0]["actor_username"] == "ankitsarin"
    assert audit[0]["actor_role"] == "admin"
    assert audit[0]["action"] == "attention.dismiss"
    assert audit[0]["target_id"] == str(item_id)
    assert audit[0]["before_value"] == "open"
    assert audit[0]["after_value"] == "dismissed"
    assert audit[0]["reason"] == "Unrecognized BDV file from operator station 3"
    assert audit[0]["resolved_on_behalf_of"] is None


def test_admin_dismiss_short_reason_rejects_without_state_change(app_env):
    item_id = _seed_attention(
        app_env, item_type="malformed_marker",
        affected_user="system_worker", case_id=None,
    )
    _, _, _, _, _, error_md = _admin_dismiss_handler(
        _admin_request(), item_id,
        "too short",  # 9 chars
        "All types", "All surgeons", "All", 0,
    )
    assert "at least 10" in error_md
    # Item still open.
    row = _read_attention_item(app_env, item_id)
    assert row["status"] == "open"
    # No audit row.
    audit = _read_audit_rows(app_env)
    assert audit == []


# ===== Resolve-on-behalf action =====


def test_admin_resolve_on_behalf_writes_correct_audit(app_env):
    """The audit row captures actor=admin AND on-behalf-of=surgeon."""
    item_id = _seed_attention(
        app_env, item_type="pipeline_failure",
        affected_user="asarin", case_id="UCD-FIL-001",
        severity="high",
    )
    _, _, _, _, _, error_md = _admin_resolve_handler(
        _admin_request(), item_id,
        "asarin",  # on_behalf_of
        "Manually retried the case via NAS recovery script",
        "All types", "All surgeons", "All", 0,
    )
    assert error_md == ""
    row = _read_attention_item(app_env, item_id)
    assert row["status"] == "resolved"
    audit = _read_audit_rows(app_env)
    assert len(audit) == 1
    assert audit[0]["actor_username"] == "ankitsarin"
    assert audit[0]["actor_role"] == "admin"
    assert audit[0]["action"] == "attention.resolve"
    assert audit[0]["resolved_on_behalf_of"] == "asarin"


def test_admin_resolve_removes_item_from_surgeon_ar(app_env):
    """After admin resolves on behalf of asarin, the surgeon's own
    list_for_user returns an empty open queue for that item."""
    from app.repos.attention import SqliteAttentionItemsRepository

    item_id = _seed_attention(
        app_env, item_type="pipeline_failure",
        affected_user="asarin", case_id="UCD-FIL-001",
        severity="high",
    )
    repo = SqliteAttentionItemsRepository()
    # Surgeon sees it before resolution.
    before = repo.list_for_user("asarin", status="open")
    assert any(i.id == item_id for i in before)

    _admin_resolve_handler(
        _admin_request(), item_id,
        "asarin",
        "Manually retried the case via NAS recovery script",
        "All types", "All surgeons", "All", 0,
    )

    after = repo.list_for_user("asarin", status="open")
    assert all(i.id != item_id for i in after), (
        "resolved item must drop out of surgeon's open AR queue"
    )


# ===== Malformed marker display label =====


def test_malformed_marker_display_label(app_env):
    """Brief #4 Step 5: the user-facing label for malformed_marker is
    'Unrecognized BDV filename'. This is the value rendered in the
    admin AR's Type column via display_for_type()."""
    td = display_for_type("malformed_marker")
    assert td.label == "Unrecognized BDV filename"
    assert td.description  # non-empty descriptive sentence


def test_malformed_marker_appears_in_admin_ar(app_env):
    """End-to-end: a malformed_marker row affected to system_worker
    surfaces in the admin AR table with the curated display label."""
    _seed_attention(
        app_env, item_type="malformed_marker",
        affected_user="system_worker", case_id=None,
        details="A submitted case could not be processed (file: .ready-foo.json)",
    )
    _, cached_rows, _ = render_ar(
        _admin_request(),
        type_filter="All types",
        surgeon_filter="All surgeons",
        severity_filter="All",
        age_filter=0,
    )
    assert len(cached_rows) == 1
    assert cached_rows[0]["type_label"] == "Unrecognized BDV filename"
    # Surgeon column falls back to the affected_user verbatim for
    # rows not owned by a folder_slug surgeon.
    assert cached_rows[0]["surgeon_label"] == "system_worker"
