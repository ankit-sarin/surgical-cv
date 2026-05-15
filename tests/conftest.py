"""Shared fixtures for the HTTP-layer test files (test_auth, test_routing,
test_scopes). The pipeline / admin-CLI tests don't import these — they use
their own tmpdir setup at the test-file level."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SECRET = "test-secret-32-bytes-or-longer-please-thank-you"
TEST_DSM_URL = "https://dsm.test.invalid/webapi/auth.cgi"
SEED_TS = "2026-05-15T00:00:00+00:00"

_CASE_MANIFEST_HEADER = (
    "ucd_fil_id,surgeon,case_year,or_room,procedure_name,approach,indication,notes"
)
_DEFAULT_CASE_MANIFEST_ROWS = (
    "UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,",
    "UCD-FIL-002,sarin,2026,OR 4,Right hemicolectomy,Robotic,Colorectal cancer,",
    "UCD-FIL-099,miller,2026,OR 1,Sigmoidectomy,Open,Colorectal cancer,",
)


def _init_and_seed(db_path: Path) -> None:
    schema_sql = (PROJECT_ROOT / "app" / "db" / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(schema_sql)
        conn.execute(
            "INSERT INTO specialties (specialty_code, display_name, active, created_at) "
            "VALUES (?, ?, 1, ?)",
            ("colorectal", "Colorectal Surgery", SEED_TS),
        )
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, active, "
            " created_at) VALUES (?, 'surgeon', ?, 'colorectal', 1, ?)",
            ("asarin", "sarin", SEED_TS),
        )
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, active, "
            " created_at) VALUES (?, 'admin', NULL, NULL, 1, ?)",
            ("ankitsarin", SEED_TS),
        )
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, active, "
            " created_at) VALUES (?, 'surgeon', ?, 'colorectal', 0, ?)",
            ("inactiveuser", "ghost", SEED_TS),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Per-test env: APP_DB_PATH at a fresh tmp DB, session secret set,
    dev-mode on, MOCK_AUTH cleared, NAS_DSM_URL fixed, CASE_MANIFEST_PATH
    pointing at a fresh tmp CSV with 3 cases (2 sarin, 1 miller)."""
    db = tmp_path / "test.db"
    _init_and_seed(db)

    manifest = tmp_path / "case_manifest.csv"
    manifest.write_text(
        _CASE_MANIFEST_HEADER
        + "\n"
        + "\n".join(_DEFAULT_CASE_MANIFEST_ROWS)
        + "\n"
    )

    monkeypatch.setenv("APP_DB_PATH", str(db))
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("APP_DEV_MODE", "1")
    monkeypatch.setenv("NAS_DSM_URL", TEST_DSM_URL)
    monkeypatch.setenv("CASE_MANIFEST_PATH", str(manifest))
    monkeypatch.delenv("MOCK_AUTH", raising=False)
    return db


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def make_dsm_mock(payload: dict):
    """Return a callable that mimics httpx.post and returns ``payload`` as JSON."""

    def _mock(url, data=None, **kwargs):
        return httpx.Response(
            200,
            json=payload,
            request=httpx.Request("POST", url),
        )

    return _mock


def patch_dsm(monkeypatch, payload_or_callable):
    """Patch ``app.auth.httpx.post``. Accepts either a dict (returned as JSON
    once) or a callable (full ``httpx.post`` replacement, e.g. for sequential
    different responses or to capture call args)."""
    target = "app.auth.httpx.post"
    if callable(payload_or_callable):
        monkeypatch.setattr(target, payload_or_callable)
    else:
        monkeypatch.setattr(target, make_dsm_mock(payload_or_callable))


def read_violations(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scope_violation_log ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
