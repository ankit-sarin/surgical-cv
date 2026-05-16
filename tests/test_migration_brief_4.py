"""Smoke test for ``scripts/migrate_brief_4_admin_schema.py``.

Runs the migration against a fixture DB built from the *previous*
schema shape (pre-Brief-#4: ``attention_items`` lacks ``updated_at``,
``admin_audit`` still has ``admin_username``). Asserts:

  - ``attention_items`` is recreated with ``updated_at`` and the partial
    unique index (recreated from schema.sql via the extract helper).
  - ``admin_audit`` retains its rows, gains ``actor_role`` and
    ``resolved_on_behalf_of``, and renames ``admin_username`` to
    ``actor_username`` without losing data.
  - A backup file is written under ``--commit``.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


_OLD_SCHEMA = """
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('surgeon', 'admin')),
    folder_slug TEXT,
    specialty TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE attention_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    case_id TEXT,
    affected_user TEXT NOT NULL REFERENCES users(username),
    severity TEXT NOT NULL DEFAULT 'normal',
    details TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(username),
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_username TEXT NOT NULL REFERENCES users(username),
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before_value TEXT,
    after_value TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _build_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, "
            " active, created_at) VALUES "
            "(?, 'surgeon', 'sarin', 'colorectal', 1, '2026-01-01T00:00:00+00:00')",
            ("asarin",),
        )
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, "
            " active, created_at) VALUES "
            "(?, 'admin', NULL, NULL, 1, '2026-01-01T00:00:00+00:00')",
            ("ankitsarin",),
        )
        # Two pre-existing admin_audit rows that the migration must
        # preserve byte-for-byte (modulo new column defaults).
        conn.execute(
            "INSERT INTO admin_audit (admin_username, action, target_kind, "
            " target_id, before_value, after_value, reason, created_at) "
            "VALUES (?, 'attention.resolve', 'attention_item', '1', "
            " 'open', 'resolved', 'historical action', "
            " '2026-04-01T12:00:00+00:00')",
            ("ankitsarin",),
        )
        conn.execute(
            "INSERT INTO admin_audit (admin_username, action, target_kind, "
            " target_id, before_value, after_value, reason, created_at) "
            "VALUES (?, 'attention.dismiss', 'attention_item', '2', "
            " 'open', 'dismissed', 'historical dismiss', "
            " '2026-04-02T13:00:00+00:00')",
            ("ankitsarin",),
        )
        conn.commit()
    finally:
        conn.close()


def _run_migration(db_path: Path, mode: str) -> subprocess.CompletedProcess:
    """Invoke the migration script as a subprocess so we exercise the
    real entry point (argparse, exit codes) rather than calling main()
    directly."""
    env = {
        "PATH": "/usr/bin:/bin",
        "APP_DB_PATH": str(db_path),
        # Tests are run with the venv python already on PATH.
    }
    return subprocess.run(
        [
            sys.executable, "-m", "app.db.migrate_brief_4",
            mode,
        ],
        cwd=_PROJECT_ROOT, capture_output=True, text=True,
        env={**env, "PYTHONPATH": str(_PROJECT_ROOT)},
        check=False,
    )


def test_migration_preserves_audit_data_and_adds_columns(tmp_path):
    db = tmp_path / "test.db"
    _build_old_db(db)

    result = _run_migration(db, "--commit")
    assert result.returncode == 0, (
        f"migration failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        # ----- attention_items shape -----
        cols = [r[1] for r in conn.execute("PRAGMA table_info(attention_items)")]
        assert "updated_at" in cols
        # Brief #3.5b partial unique index recreated.
        indexes = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='attention_items'"
            )
        ]
        assert "idx_attention_phi_redacted_case_uniq" in indexes

        # ----- admin_audit shape -----
        cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_audit)")}
        # Rename landed.
        assert "actor_username" in cols
        assert "admin_username" not in cols
        # New columns landed.
        assert "actor_role" in cols
        assert "resolved_on_behalf_of" in cols

        # ----- data preserved -----
        rows = list(conn.execute(
            "SELECT * FROM admin_audit ORDER BY id"
        ).fetchall())
        assert len(rows) == 2
        # First row's actor_username preserves the renamed admin_username.
        assert rows[0]["actor_username"] == "ankitsarin"
        assert rows[0]["actor_role"] == "admin"  # backfilled
        assert rows[0]["resolved_on_behalf_of"] is None
        assert rows[0]["reason"] == "historical action"
        assert rows[0]["action"] == "attention.resolve"
        # Second row.
        assert rows[1]["actor_username"] == "ankitsarin"
        assert rows[1]["actor_role"] == "admin"
        assert rows[1]["reason"] == "historical dismiss"
    finally:
        conn.close()

    # Backup file written next to the live DB.
    bak_files = list(tmp_path.glob("test.db.pre-brief-4.*.bak"))
    assert len(bak_files) == 1, f"expected 1 backup, got {bak_files!r}"


def test_migration_dry_run_makes_no_changes(tmp_path):
    db = tmp_path / "test.db"
    _build_old_db(db)
    snapshot_before = db.read_bytes()

    result = _run_migration(db, "--dry-run")
    assert result.returncode == 0
    # Database bytes unchanged.
    assert db.read_bytes() == snapshot_before
    # No backup written.
    assert not list(tmp_path.glob("test.db.pre-brief-4.*.bak"))
    # Plan is in stdout.
    assert "attention_items" in result.stdout
    assert "admin_audit" in result.stdout
    assert "(dry run" in result.stdout
