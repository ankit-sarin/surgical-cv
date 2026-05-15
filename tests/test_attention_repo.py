"""Tests for ``app/repos/attention.py`` — the minimal "does this case
have any attention items?" surface used by the surgeon My Cases badge
derivation.

Schema-backed integration goes through the ``app_env`` fixture (real
SQLite tmp DB initialized from ``schema.sql``); the in-memory fake's
parity tests don't need any fixtures.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.connection import connect, utcnow
from app.repos.attention import (
    InMemoryAttentionItemsRepository,
    SqliteAttentionItemsRepository,
)


_SEED_TS = "2026-05-15T00:00:00+00:00"


def _seed_attention(
    db_path,
    *,
    case_id: str,
    affected_user: str = "asarin",
    item_type: str = "phi_warning",
    severity: str = "normal",
    details: str = "x",
    status: str = "open",
) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.execute(
            "INSERT INTO attention_items "
            "(type, case_id, affected_user, severity, details, "
            " created_at, created_by, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_type,
                case_id,
                affected_user,
                severity,
                details,
                utcnow(),
                affected_user,
                status,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ----- SqliteAttentionItemsRepository -----


def test_sqlite_happy_path_marks_only_flagged_cases(app_env):
    _seed_attention(app_env, case_id="UCD-FIL-001")
    _seed_attention(app_env, case_id="UCD-FIL-001", item_type="other_flag")
    repo = SqliteAttentionItemsRepository()
    out = repo.has_attention_for_case_ids(["UCD-FIL-001", "UCD-FIL-002"])
    assert out == {"UCD-FIL-001": True, "UCD-FIL-002": False}


def test_sqlite_empty_input_returns_empty(app_env):
    repo = SqliteAttentionItemsRepository()
    assert repo.has_attention_for_case_ids([]) == {}


def test_sqlite_distinct_collapses_repeats(app_env):
    """Three rows for one case still report as a single True — the
    surgeon UI cares about presence/absence, not count."""
    for _ in range(3):
        _seed_attention(app_env, case_id="UCD-FIL-001")
    repo = SqliteAttentionItemsRepository()
    out = repo.has_attention_for_case_ids(["UCD-FIL-001"])
    assert out == {"UCD-FIL-001": True}


def test_sqlite_unknown_case_id_is_false(app_env):
    """A case_id that's never appeared in the table reports False, not
    omitted from the dict."""
    _seed_attention(app_env, case_id="UCD-FIL-001")
    repo = SqliteAttentionItemsRepository()
    out = repo.has_attention_for_case_ids(
        ["UCD-FIL-001", "UCD-FIL-099", "UCD-FIL-444"]
    )
    assert out == {
        "UCD-FIL-001": True,
        "UCD-FIL-099": False,
        "UCD-FIL-444": False,
    }


def test_sqlite_resolved_status_still_counts(app_env):
    """Per the repo docstring: status filter is intentionally absent.
    Resolved attention still represents reviewer attention the case
    received, so the My Cases badge still reads as flagged."""
    _seed_attention(app_env, case_id="UCD-FIL-001", status="resolved")
    repo = SqliteAttentionItemsRepository()
    assert repo.has_attention_for_case_ids(["UCD-FIL-001"]) == {
        "UCD-FIL-001": True,
    }


def test_sqlite_missing_db_file_yields_all_false(monkeypatch, tmp_path):
    """No DB file → every case_id reports False (never raises). Matches
    the CSV repos' missing-file shape."""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "absent.db"))
    repo = SqliteAttentionItemsRepository()
    out = repo.has_attention_for_case_ids(["UCD-FIL-001", "UCD-FIL-002"])
    assert out == {"UCD-FIL-001": False, "UCD-FIL-002": False}


def test_sqlite_query_uses_parameterized_placeholders(app_env, monkeypatch):
    """Defense in depth: confirm the SQL is parameterized — case_ids with
    SQL metacharacters must not blow up the query or produce false
    positives."""
    _seed_attention(app_env, case_id="UCD-FIL-001")
    repo = SqliteAttentionItemsRepository()
    weird = ["UCD-FIL-001", "'; DROP TABLE attention_items; --"]
    out = repo.has_attention_for_case_ids(weird)
    assert out["UCD-FIL-001"] is True
    assert out["'; DROP TABLE attention_items; --"] is False
    # Table still there.
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM attention_items"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


# ----- InMemoryAttentionItemsRepository -----


def test_inmem_marks_flagged_cases_only():
    repo = InMemoryAttentionItemsRepository({"UCD-FIL-001"})
    assert repo.has_attention_for_case_ids(
        ["UCD-FIL-001", "UCD-FIL-002"]
    ) == {"UCD-FIL-001": True, "UCD-FIL-002": False}


def test_inmem_empty_input_returns_empty():
    repo = InMemoryAttentionItemsRepository({"UCD-FIL-001"})
    assert repo.has_attention_for_case_ids([]) == {}


def test_inmem_default_no_flags():
    repo = InMemoryAttentionItemsRepository()
    assert repo.has_attention_for_case_ids(["UCD-FIL-001"]) == {
        "UCD-FIL-001": False,
    }


@pytest.mark.parametrize("flagged", [{"UCD-FIL-001"}, set()])
def test_inmem_parity_with_sqlite_for_unknown_ids(flagged):
    """In-memory and SQLite must agree on the "False for unknowns" shape
    so swapping fakes for production doesn't shift behaviour."""
    repo = InMemoryAttentionItemsRepository(flagged)
    out = repo.has_attention_for_case_ids(["UCD-FIL-999"])
    assert out == {"UCD-FIL-999": False}
