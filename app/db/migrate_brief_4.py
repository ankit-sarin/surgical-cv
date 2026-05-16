"""One-shot migration: admin_audit rename + attention_items.updated_at.

Brief #4 bundles two schema changes that must land in one deploy:

  A. ``attention_items`` — add ``updated_at`` column (Brief #3.5b shipped
     the schema.sql change, but never ran a live migration). The table
     is empty in production today (verified per the brief's pre-flight
     read-first), so the chosen path is drop-and-recreate from the
     current ``schema.sql``. This also reinstates the partial unique
     index ``idx_attention_phi_redacted_case_uniq`` and any auxiliary
     indexes the canonical schema declares.

  B. ``admin_audit`` — column rename and additions:
       - ``admin_username`` → ``actor_username``  (preserves data; ALTER
         TABLE RENAME COLUMN is data-preserving in SQLite ≥ 3.25).
       - new column ``actor_role TEXT NOT NULL`` (one of 'surgeon' /
         'admin'). Existing rows backfill to ``'admin'`` since pre-Brief-#4
         the table was admin-only by intent — there was no surgeon
         self-service write path yet.
       - new column ``resolved_on_behalf_of TEXT NULL REFERENCES
         users(username)``. NULL for all backfilled rows.

Migration tooling context
-------------------------

The repo has no general migration runner (no Alembic, no
``schema_versions`` table, no ``PRAGMA user_version`` convention). Per
Brief #4 Step 1, the chosen path follows the existing
``scripts/migrate_*.py`` precedent — a Python one-shot with
``--dry-run`` / ``--commit`` modes. A general migration tool is
explicitly out of scope (Brief #5).

Idempotency
-----------

This script is **not** idempotent — running ``--commit`` twice will
fail at the column-add step (SQLite raises if the column already
exists). That's intentional: an idempotent migration disguises a
re-run, which usually means a broken automation loop somewhere.
``--dry-run`` is always safe to re-run.

Reversibility
-------------

Before any DDL fires under ``--commit``, the live ``app.db`` is copied
to ``<APP_DB_PATH>.pre-brief-4.<utc-ts>.bak``. To roll back: stop the
service, ``mv`` the backup over the live DB, restart. The bakup is
plain bytes — no SQLite-specific tooling needed.

Usage
-----

    python -m app.db.migrate_brief_4 --dry-run        # preview
    python -m app.db.migrate_brief_4 --commit         # apply

Reads ``APP_DB_PATH`` for the target DB (honors the same env var as
``app/db/connection.py``); falls back to ``app/db/app.db`` relative to
the repo root.

Location note: lives under ``app/db/`` alongside ``init_db.py`` because
``scripts/`` is gitignored project-wide (the existing
``scripts/migrate_manifest_spec_j.py`` is a local-only operator file).
A migration is a reviewable artifact — Brief #4 calls it out
explicitly — so it belongs in tracked source.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


_APP_DB_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = _APP_DB_DIR / "app.db"
SCHEMA_PATH = _APP_DB_DIR / "schema.sql"


def _resolve_db_path() -> Path:
    env = os.environ.get("APP_DB_PATH")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def _extract_block(schema_sql: str, leading_token: str, terminator: str) -> str:
    """Pull a single statement block out of schema.sql by literal-prefix
    match + first-terminator-after. Used to mirror the canonical
    ``CREATE TABLE attention_items (...)`` and its companion
    ``CREATE UNIQUE INDEX`` exactly — avoids drift between this migration
    and the schema source of truth."""
    idx = schema_sql.find(leading_token)
    if idx == -1:
        raise RuntimeError(
            f"schema.sql does not contain the expected token "
            f"{leading_token!r}; migration cannot proceed safely."
        )
    end = schema_sql.find(terminator, idx)
    if end == -1:
        raise RuntimeError(
            f"schema.sql token {leading_token!r} not terminated by "
            f"{terminator!r}; migration cannot proceed safely."
        )
    return schema_sql[idx:end + len(terminator)]


def _attention_table_block(schema_sql: str) -> str:
    """``CREATE TABLE attention_items (...)`` body."""
    return _extract_block(schema_sql, "CREATE TABLE attention_items", ");")


def _attention_indexes_block(schema_sql: str) -> str:
    """All ``CREATE INDEX`` / ``CREATE UNIQUE INDEX`` statements that
    target ``attention_items``. Built by scanning the file for ``ON
    attention_items`` and walking back to the preceding ``CREATE`` and
    forward to the next ``;``."""
    pieces: list[str] = []
    cursor = 0
    while True:
        anchor = schema_sql.find("ON attention_items", cursor)
        if anchor == -1:
            break
        # Walk backward to the preceding ``CREATE``.
        start = schema_sql.rfind("CREATE", 0, anchor)
        if start == -1:
            raise RuntimeError(
                "found ``ON attention_items`` with no preceding ``CREATE``"
            )
        # Walk forward to the terminating ``;``.
        end = schema_sql.find(";", anchor)
        if end == -1:
            raise RuntimeError(
                "``ON attention_items`` clause not terminated by ``;``"
            )
        pieces.append(schema_sql[start:end + 1])
        cursor = end + 1
    return "\n".join(pieces)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _ts_for_backup() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _plan_attention_changes(conn: sqlite3.Connection) -> dict:
    """Decide what attention_items needs. Returns a dict describing the
    plan in human-readable terms (printed under --dry-run) plus the SQL
    that will execute under --commit."""
    if not _table_exists(conn, "attention_items"):
        return {
            "action": "create",
            "note": "table absent — will create from schema.sql",
            "rows_preserved": 0,
        }
    cols = _column_names(conn, "attention_items")
    rows = _row_count(conn, "attention_items")
    if "updated_at" in cols:
        return {
            "action": "noop",
            "note": "updated_at column already present — no change",
            "rows_preserved": rows,
        }
    # Per brief: drop-and-recreate is safe iff zero rows. Bail loudly
    # otherwise so an operator never silently loses data.
    if rows > 0:
        raise RuntimeError(
            f"attention_items has {rows} row(s) and is missing the "
            f"updated_at column. Drop-and-recreate would lose data. "
            f"Refusing to proceed — investigate before re-running."
        )
    return {
        "action": "drop_and_recreate",
        "note": "table empty — will DROP and recreate from schema.sql",
        "rows_preserved": 0,
    }


def _plan_admin_audit_changes(conn: sqlite3.Connection) -> dict:
    if not _table_exists(conn, "admin_audit"):
        # Fresh DB. Nothing to migrate — init_db will have created the
        # current shape.
        return {
            "action": "noop",
            "note": "admin_audit table absent (fresh init_db) — no change",
            "rows_preserved": 0,
            "needs_rename": False,
            "needs_actor_role": False,
            "needs_on_behalf_of": False,
        }
    cols = _column_names(conn, "admin_audit")
    rows = _row_count(conn, "admin_audit")
    return {
        "action": "alter_in_place",
        "note": (
            f"will ALTER admin_audit "
            f"({rows} existing row(s) backfilled to actor_role='admin')"
        ),
        "rows_preserved": rows,
        "needs_rename": "admin_username" in cols and "actor_username" not in cols,
        "needs_actor_role": "actor_role" not in cols,
        "needs_on_behalf_of": "resolved_on_behalf_of" not in cols,
    }


def _execute_attention(conn: sqlite3.Connection, plan: dict, schema_sql: str):
    if plan["action"] == "noop":
        return
    if plan["action"] in ("create", "drop_and_recreate"):
        if plan["action"] == "drop_and_recreate":
            conn.execute("DROP TABLE attention_items")
        # Re-create from canonical schema.sql blocks so the migration
        # tracks the schema source of truth automatically — no risk of
        # drift between a hand-written CREATE TABLE here and the real one.
        conn.executescript(_attention_table_block(schema_sql))
        idx_sql = _attention_indexes_block(schema_sql)
        if idx_sql.strip():
            conn.executescript(idx_sql)
        return
    raise RuntimeError(f"unknown attention plan action: {plan['action']!r}")


def _execute_admin_audit(conn: sqlite3.Connection, plan: dict):
    if plan["action"] == "noop":
        return
    if plan["needs_rename"]:
        conn.execute(
            "ALTER TABLE admin_audit RENAME COLUMN admin_username TO actor_username"
        )
    if plan["needs_actor_role"]:
        # Backfill DEFAULT 'admin' covers existing rows (all pre-Brief-#4
        # audit rows were admin actions by intent — no surgeon
        # self-service path existed). The DEFAULT lives only on this
        # ALTER; schema.sql does not declare it because new inserts
        # MUST specify actor_role explicitly going forward.
        conn.execute(
            "ALTER TABLE admin_audit ADD COLUMN actor_role TEXT NOT NULL "
            "DEFAULT 'admin' CHECK (actor_role IN ('surgeon', 'admin'))"
        )
    if plan["needs_on_behalf_of"]:
        # Nullable FK to users.username; existing rows backfill to NULL.
        # No DEFAULT needed — NULL is the natural absence value.
        conn.execute(
            "ALTER TABLE admin_audit ADD COLUMN resolved_on_behalf_of TEXT "
            "REFERENCES users(username)"
        )


def _format_plan(plan_a: dict, plan_b: dict, db_path: Path) -> str:
    lines: list[str] = []
    lines.append(f"Target DB: {db_path}")
    lines.append("")
    lines.append("attention_items:")
    lines.append(f"  action: {plan_a['action']}")
    lines.append(f"  note:   {plan_a['note']}")
    lines.append(f"  rows preserved: {plan_a['rows_preserved']}")
    lines.append("")
    lines.append("admin_audit:")
    lines.append(f"  action: {plan_b['action']}")
    lines.append(f"  note:   {plan_b['note']}")
    if plan_b["action"] != "noop":
        lines.append(f"  rename admin_username → actor_username: {plan_b['needs_rename']}")
        lines.append(f"  add actor_role (NOT NULL, DEFAULT 'admin' for backfill): {plan_b['needs_actor_role']}")
        lines.append(f"  add resolved_on_behalf_of (NULL): {plan_b['needs_on_behalf_of']}")
        lines.append(f"  rows preserved: {plan_b['rows_preserved']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.migrate_brief_4_admin_schema",
        description=__doc__.split("\n\n")[0],
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="print the migration plan without touching the database",
    )
    group.add_argument(
        "--commit",
        action="store_true",
        help="apply the migration. Creates a .bak first.",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db_path()
    if not db_path.exists():
        print(
            f"error: target database {db_path} does not exist. "
            f"Run ``python -m app.db.init_db`` first.",
            file=sys.stderr,
        )
        return 1

    schema_sql = SCHEMA_PATH.read_text()

    # Connect read-only-ish first to compute the plan, then re-open for
    # write if --commit. (PRAGMA foreign_keys is OFF for ALTER work —
    # SQLite has known quirks with FK validation mid-ALTER. We re-enable
    # at the end and validate.)
    conn = sqlite3.connect(db_path)
    try:
        plan_a = _plan_attention_changes(conn)
        plan_b = _plan_admin_audit_changes(conn)
    finally:
        conn.close()

    print(_format_plan(plan_a, plan_b, db_path))

    if args.dry_run:
        print("\n(dry run — no changes applied)")
        return 0

    # --commit path.
    backup_path = db_path.with_suffix(
        db_path.suffix + f".pre-brief-4.{_ts_for_backup()}.bak"
    )
    shutil.copy2(db_path, backup_path)
    print(f"\nbackup written to: {backup_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            _execute_attention(conn, plan_a, schema_sql)
            _execute_admin_audit(conn, plan_b)
        # Re-enable FK enforcement and sanity-check (any orphaned rows
        # would surface here — none expected, but explicit is better).
        conn.execute("PRAGMA foreign_keys = ON")
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            print(
                f"WARNING: foreign_key_check reported violations after "
                f"migration: {fk_violations}",
                file=sys.stderr,
            )
            return 2
    finally:
        conn.close()

    print("migration applied successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
