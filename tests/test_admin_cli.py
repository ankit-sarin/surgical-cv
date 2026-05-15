"""Hermetic tests for app/db/init_db.py and app/db/admin_cli.py.

Each test gets a fresh tmpdir + APP_DB_PATH. Subcommands run as subprocesses
(matches the existing test_cli_metadata.py pattern). DB state is asserted via
direct sqlite3 reads, not via the CLI's text output.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _admin(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "app.db.admin_cli", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _init(db_path: Path, *, force: bool = False, env_extra: dict | None = None):
    env = {**os.environ, "APP_DB_PATH": str(db_path)}
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, "-m", "app.db.init_db"]
    if force:
        cmd.append("--force")
    return subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=env
    )


def _env(db_path: Path, **extra) -> dict:
    return {**os.environ, "APP_DB_PATH": str(db_path), **extra}


def _query(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@pytest.fixture
def fresh_db(tmp_path):
    db = tmp_path / "test.db"
    r = _init(db)
    assert r.returncode == 0, r.stderr
    return db, _env(db)


@pytest.fixture
def db_with_specialty(fresh_db):
    db, env = fresh_db
    r = _admin(
        "specialty", "add", "colorectal",
        "--display-name", "Colorectal Surgery", env=env,
    )
    assert r.returncode == 0, r.stderr
    return db, env


# ============================================================
# init_db
# ============================================================


def test_init_creates_db_file(tmp_path):
    db = tmp_path / "fresh.db"
    r = _init(db)
    assert r.returncode == 0, r.stderr
    assert db.exists()


def test_init_creates_all_six_tables(fresh_db):
    db, _ = fresh_db
    rows = _query(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    names = {r["name"] for r in rows}
    assert names == {
        "admin_audit",
        "attention_items",
        "picklist_values",
        "scope_violation_log",
        "specialties",
        "users",
    }


def test_init_creates_expected_indexes(fresh_db):
    db, _ = fresh_db
    rows = _query(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    names = {r["name"] for r in rows}
    expected = {
        "idx_admin_audit_created",
        "idx_attention_affected_status",
        "idx_attention_status_created",
        "idx_picklist_dropdown",
        "idx_users_role",
        "idx_users_specialty",
        "idx_violation_user_created",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_init_refuses_overwrite_without_force(fresh_db):
    db, _ = fresh_db
    r = _init(db)  # no --force
    assert r.returncode == 1
    assert "already exists" in r.stderr
    assert db.exists()


def test_init_overwrites_with_force(fresh_db):
    db, env = fresh_db
    _admin("specialty", "add", "x", "--display-name", "X", env=env)
    r = _init(db, force=True)
    assert r.returncode == 0
    rows = _query(db, "SELECT COUNT(*) AS n FROM specialties")
    assert rows[0]["n"] == 0  # forced reinit wiped seeded row


def test_init_enforces_foreign_keys_on_connection(fresh_db):
    """Ensure connection-level PRAGMA foreign_keys=ON survives by attempting
    an FK violation through the CLI (user with unknown specialty)."""
    db, env = fresh_db
    # No specialty seeded; surgeon insert should bail on FK existence check.
    r = _admin(
        "user", "add", "alice",
        "--role", "surgeon",
        "--folder-slug", "alice",
        "--specialty", "ghost",
        env=env,
    )
    assert r.returncode == 1
    assert "ghost" in r.stderr


# ============================================================
# specialty
# ============================================================


def test_specialty_add_success(fresh_db):
    db, env = fresh_db
    r = _admin(
        "specialty", "add", "colorectal",
        "--display-name", "Colorectal Surgery", env=env,
    )
    assert r.returncode == 0, r.stderr
    rows = _query(db, "SELECT * FROM specialties WHERE specialty_code = 'colorectal'")
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Colorectal Surgery"
    assert rows[0]["active"] == 1
    assert rows[0]["created_at"]


def test_specialty_add_duplicate_fails(db_with_specialty):
    _, env = db_with_specialty
    r = _admin(
        "specialty", "add", "colorectal",
        "--display-name", "Colorectal Again", env=env,
    )
    assert r.returncode == 1
    assert "UNIQUE" in r.stderr or "exists" in r.stderr.lower()


def test_specialty_list_after_add(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("specialty", "list", env=env)
    assert r.returncode == 0
    assert "colorectal" in r.stdout
    assert "Colorectal Surgery" in r.stdout


def test_specialty_list_empty(fresh_db):
    _, env = fresh_db
    r = _admin("specialty", "list", env=env)
    assert r.returncode == 0
    assert "(no specialties)" in r.stdout


# ============================================================
# user add
# ============================================================


def test_user_add_surgeon_success(db_with_specialty):
    db, env = db_with_specialty
    r = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        "--specialty", "colorectal",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    rows = _query(db, "SELECT * FROM users WHERE username = 'asarin'")
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "surgeon"
    assert row["folder_slug"] == "sarin"
    assert row["specialty"] == "colorectal"
    assert row["active"] == 1
    assert row["display_name"] is None
    assert row["email"] is None


def test_user_add_admin_success(fresh_db):
    db, env = fresh_db
    r = _admin("user", "add", "ankitsarin", "--role", "admin", env=env)
    assert r.returncode == 0, r.stderr
    rows = _query(db, "SELECT * FROM users WHERE username = 'ankitsarin'")
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "admin"
    assert row["folder_slug"] is None
    assert row["specialty"] is None


def test_user_add_surgeon_missing_folder_slug_rejected(db_with_specialty):
    _, env = db_with_specialty
    r = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--specialty", "colorectal",
        env=env,
    )
    assert r.returncode == 1
    assert "folder-slug" in r.stderr


def test_user_add_surgeon_missing_specialty_rejected(db_with_specialty):
    _, env = db_with_specialty
    r = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        env=env,
    )
    assert r.returncode == 1
    assert "specialty" in r.stderr


def test_user_add_admin_with_folder_slug_rejected(fresh_db):
    _, env = fresh_db
    r = _admin(
        "user", "add", "rogueadmin",
        "--role", "admin",
        "--folder-slug", "rogue",
        env=env,
    )
    assert r.returncode == 1
    assert "admin" in r.stderr.lower()


def test_user_add_admin_with_specialty_rejected(db_with_specialty):
    _, env = db_with_specialty
    r = _admin(
        "user", "add", "rogueadmin",
        "--role", "admin",
        "--specialty", "colorectal",
        env=env,
    )
    assert r.returncode == 1
    assert "admin" in r.stderr.lower()


def test_user_add_surgeon_unknown_specialty_rejected(fresh_db):
    _, env = fresh_db
    r = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        "--specialty", "nonexistent",
        env=env,
    )
    assert r.returncode == 1
    assert "nonexistent" in r.stderr


def test_user_add_duplicate_username_rejected(db_with_specialty):
    db, env = db_with_specialty
    r1 = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        "--specialty", "colorectal",
        env=env,
    )
    assert r1.returncode == 0
    r2 = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        "--specialty", "colorectal",
        env=env,
    )
    assert r2.returncode == 1


def test_user_add_with_email_and_display_name(db_with_specialty):
    db, env = db_with_specialty
    r = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        "--specialty", "colorectal",
        "--display-name", "Ankit Sarin, MD",
        "--email", "asarin@example.org",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    row = _query(db, "SELECT * FROM users WHERE username = 'asarin'")[0]
    assert row["display_name"] == "Ankit Sarin, MD"
    assert row["email"] == "asarin@example.org"


# ============================================================
# user update
# ============================================================


@pytest.fixture
def db_with_surgeon(db_with_specialty):
    db, env = db_with_specialty
    r = _admin(
        "user", "add", "asarin",
        "--role", "surgeon",
        "--folder-slug", "sarin",
        "--specialty", "colorectal",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    return db, env


def test_user_update_display_name(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "display_name", "Dr. A Sarin", env=env)
    assert r.returncode == 0, r.stderr
    row = _query(db, "SELECT display_name FROM users WHERE username = 'asarin'")[0]
    assert row["display_name"] == "Dr. A Sarin"


def test_user_update_email(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "email", "ankit@example.org", env=env)
    assert r.returncode == 0
    row = _query(db, "SELECT email FROM users WHERE username = 'asarin'")[0]
    assert row["email"] == "ankit@example.org"


def test_user_update_notes(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "notes", "PI; colorectal lead", env=env)
    assert r.returncode == 0
    row = _query(db, "SELECT notes FROM users WHERE username = 'asarin'")[0]
    assert row["notes"] == "PI; colorectal lead"


def test_user_update_active_zero(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "active", "0", env=env)
    assert r.returncode == 0
    row = _query(db, "SELECT active FROM users WHERE username = 'asarin'")[0]
    assert row["active"] == 0


def test_user_update_role_rejected(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "role", "admin", env=env)
    assert r.returncode == 1
    assert "immutable" in r.stderr
    row = _query(db, "SELECT role FROM users WHERE username = 'asarin'")[0]
    assert row["role"] == "surgeon"


def test_user_update_folder_slug_rejected(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "folder_slug", "newslug", env=env)
    assert r.returncode == 1
    assert "immutable" in r.stderr
    row = _query(db, "SELECT folder_slug FROM users WHERE username = 'asarin'")[0]
    assert row["folder_slug"] == "sarin"


def test_user_update_specialty_rejected(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "specialty", "bariatric", env=env)
    assert r.returncode == 1
    assert "immutable" in r.stderr


def test_user_update_unknown_field_rejected(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "update", "asarin", "favorite_color", "teal", env=env)
    assert r.returncode == 1
    assert "unknown field" in r.stderr


def test_user_update_unknown_user(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("user", "update", "ghost", "display_name", "X", env=env)
    assert r.returncode == 1
    assert "not found" in r.stderr


# ============================================================
# user list / show / deactivate
# ============================================================


def test_user_list_shows_added_users(db_with_surgeon):
    _, env = db_with_surgeon
    _admin("user", "add", "admin1", "--role", "admin", env=env)
    r = _admin("user", "list", env=env)
    assert r.returncode == 0
    assert "asarin" in r.stdout
    assert "admin1" in r.stdout


def test_user_list_filter_by_role(db_with_surgeon):
    _, env = db_with_surgeon
    _admin("user", "add", "admin1", "--role", "admin", env=env)
    r = _admin("user", "list", "--role", "admin", env=env)
    assert r.returncode == 0
    assert "admin1" in r.stdout
    assert "asarin" not in r.stdout


def test_user_list_excludes_inactive_by_default(db_with_surgeon):
    _, env = db_with_surgeon
    _admin("user", "deactivate", "asarin", env=env)
    r = _admin("user", "list", env=env)
    assert r.returncode == 0
    assert "asarin" not in r.stdout
    r2 = _admin("user", "list", "--inactive", env=env)
    assert "asarin" in r2.stdout


def test_user_show_success(db_with_surgeon):
    _, env = db_with_surgeon
    r = _admin("user", "show", "asarin", env=env)
    assert r.returncode == 0
    assert "username: asarin" in r.stdout
    assert "role: surgeon" in r.stdout
    assert "folder_slug: sarin" in r.stdout
    assert "specialty: colorectal" in r.stdout


def test_user_show_unknown(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("user", "show", "ghost", env=env)
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_user_deactivate_sets_active_zero(db_with_surgeon):
    db, env = db_with_surgeon
    r = _admin("user", "deactivate", "asarin", env=env)
    assert r.returncode == 0
    row = _query(db, "SELECT active FROM users WHERE username = 'asarin'")[0]
    assert row["active"] == 0


def test_user_deactivate_unknown(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("user", "deactivate", "ghost", env=env)
    assert r.returncode == 1


# ============================================================
# picklist add / list / deactivate
# ============================================================


def test_picklist_add_success(db_with_specialty):
    db, env = db_with_specialty
    r = _admin(
        "picklist", "add", "procedure", "Sigmoidectomy",
        "--specialty", "colorectal",
        "--sort-order", "30",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    row = _query(
        db,
        "SELECT * FROM picklist_values "
        "WHERE field = 'procedure' AND value = 'Sigmoidectomy'",
    )[0]
    assert row["display_label"] == "Sigmoidectomy"
    assert row["sort_order"] == 30
    assert row["specialty"] == "colorectal"


def test_picklist_add_unknown_specialty_rejected(fresh_db):
    _, env = fresh_db
    r = _admin(
        "picklist", "add", "procedure", "Sigmoidectomy",
        "--specialty", "ghost", env=env,
    )
    assert r.returncode == 1
    assert "ghost" in r.stderr


def test_picklist_list_shows_added(db_with_specialty):
    _, env = db_with_specialty
    _admin(
        "picklist", "add", "procedure", "Sigmoidectomy",
        "--specialty", "colorectal", env=env,
    )
    r = _admin("picklist", "list", "procedure", env=env)
    assert r.returncode == 0
    assert "Sigmoidectomy" in r.stdout


def test_picklist_deactivate(db_with_specialty):
    db, env = db_with_specialty
    _admin(
        "picklist", "add", "procedure", "Sigmoidectomy",
        "--specialty", "colorectal", env=env,
    )
    r = _admin("picklist", "deactivate", "procedure", "Sigmoidectomy", env=env)
    assert r.returncode == 0
    row = _query(
        db,
        "SELECT active FROM picklist_values "
        "WHERE field = 'procedure' AND value = 'Sigmoidectomy'",
    )[0]
    assert row["active"] == 0


def test_picklist_deactivate_unknown(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("picklist", "deactivate", "procedure", "ghost", env=env)
    assert r.returncode == 1
    assert "not found" in r.stderr


# ============================================================
# picklist seed
# ============================================================


def _write_seed(picklist_dir: Path, name: str, payload) -> Path:
    picklist_dir.mkdir(parents=True, exist_ok=True)
    target = picklist_dir / name
    if isinstance(payload, str):
        target.write_text(payload)
    else:
        target.write_text(json.dumps(payload))
    return target


def _good_seed(field: str, specialty: str, values: list[dict] | None = None) -> dict:
    if values is None:
        values = [
            {"value": "A", "display_label": "A", "sort_order": 10},
            {"value": "B", "display_label": "B", "sort_order": 20},
            {"value": "Other", "display_label": "Other", "sort_order": 999},
        ]
    return {"field": field, "specialty": specialty, "values": values}


def test_picklist_seed_inserts_all_rows(db_with_specialty, tmp_path):
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("procedure", "colorectal"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r.returncode == 0, r.stderr
    rows = _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='procedure'")
    assert rows[0]["n"] == 3
    assert "inserted=3" in r.stdout
    assert "skipped=0" in r.stdout


def test_picklist_seed_idempotent(db_with_specialty, tmp_path):
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("procedure", "colorectal"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r1 = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r1.returncode == 0
    r2 = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r2.returncode == 0
    assert "inserted=0" in r2.stdout
    assert "skipped=3" in r2.stdout
    rows = _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='procedure'")
    assert rows[0]["n"] == 3


def test_picklist_seed_unknown_specialty(fresh_db, tmp_path):
    _, env = fresh_db
    seed_dir = tmp_path / "picklists"
    seed_dir.mkdir(parents=True)
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "ghost", env=env_seed)
    assert r.returncode == 1
    assert "ghost" in r.stderr


def test_picklist_seed_field_filter(db_with_specialty, tmp_path):
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("procedure", "colorectal"))
    _write_seed(
        seed_dir, "approach_colorectal.json",
        _good_seed("approach", "colorectal",
                   values=[{"value": "Open", "display_label": "Open", "sort_order": 10}]),
    )
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin(
        "picklist", "seed",
        "--specialty", "colorectal", "--field", "procedure",
        env=env_seed,
    )
    assert r.returncode == 0, r.stderr
    proc = _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='procedure'")
    appr = _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='approach'")
    assert proc[0]["n"] == 3
    assert appr[0]["n"] == 0


def test_picklist_seed_missing_field_file(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    seed_dir.mkdir(parents=True)
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin(
        "picklist", "seed",
        "--specialty", "colorectal", "--field", "procedure",
        env=env_seed,
    )
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_picklist_seed_no_matching_files(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    seed_dir.mkdir(parents=True)
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r.returncode == 1
    assert "no seed files" in r.stderr


def test_picklist_seed_field_mismatch(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    # Filename field is "procedure" but JSON declares "approach".
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("approach", "colorectal"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r.returncode == 1
    assert "JSON field" in r.stderr
    assert "filename" in r.stderr


def test_picklist_seed_specialty_mismatch(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("procedure", "bariatric"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r.returncode == 1
    # After Spec D the filename-vs-JSON check fires first (catches the same
    # bug with a more diagnostic message).
    assert "filename does not match" in r.stderr
    assert "bariatric" in r.stderr


def test_picklist_seed_malformed_json(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", "{not valid json")
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r.returncode == 1
    assert "failed to read" in r.stderr or "Expecting" in r.stderr


def test_picklist_seed_missing_required_key(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    bad = {
        "field": "procedure",
        "specialty": "colorectal",
        "values": [{"value": "A", "display_label": "A"}],  # missing sort_order
    }
    _write_seed(seed_dir, "procedure_colorectal.json", bad)
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--specialty", "colorectal", env=env_seed)
    assert r.returncode == 1
    assert "sort_order" in r.stderr


# ============================================================
# picklist seed — --universal mode
# ============================================================


def _good_universal_seed(field: str, values: list[dict] | None = None) -> dict:
    if values is None:
        values = [
            {"value": "Alpha", "display_label": "Alpha", "sort_order": 10},
            {"value": "Beta", "display_label": "Beta", "sort_order": 20},
        ]
    return {"field": field, "specialty": None, "values": values}


def test_picklist_seed_universal_inserts_all_rows(db_with_specialty, tmp_path):
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "approach.json", _good_universal_seed("approach"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--universal", env=env_seed)
    assert r.returncode == 0, r.stderr
    rows = _query(
        db, "SELECT value, specialty FROM picklist_values WHERE field='approach' ORDER BY sort_order"
    )
    assert [r["value"] for r in rows] == ["Alpha", "Beta"]
    assert all(r["specialty"] is None for r in rows)


def test_picklist_seed_universal_with_field_filter(db_with_specialty, tmp_path):
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "approach.json", _good_universal_seed("approach"))
    _write_seed(seed_dir, "case_year.json", _good_universal_seed("case_year"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin(
        "picklist", "seed", "--universal", "--field", "approach", env=env_seed,
    )
    assert r.returncode == 0, r.stderr
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='approach'")[0]["n"] == 2
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='case_year'")[0]["n"] == 0


def test_picklist_seed_universal_no_files_returns_1(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    seed_dir.mkdir()
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--universal", env=env_seed)
    assert r.returncode == 1
    assert "universal" in r.stderr.lower()


def test_picklist_seed_universal_skips_specialty_scoped_files(
    db_with_specialty, tmp_path
):
    """--universal must NOT ingest *_<specialty>.json files."""
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "approach.json", _good_universal_seed("approach"))
    _write_seed(
        seed_dir, "procedure_colorectal.json",
        {
            "field": "procedure",
            "specialty": "colorectal",
            "values": [{"value": "X", "display_label": "X", "sort_order": 10}],
        },
    )
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--universal", env=env_seed)
    assert r.returncode == 0, r.stderr
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='approach'")[0]["n"] == 2
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='procedure'")[0]["n"] == 0


def test_picklist_seed_universal_filename_mismatch_rejected(
    db_with_specialty, tmp_path
):
    """JSON specialty=null but the filename has a specialty suffix → caught."""
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    # Write under a misleading name; --all forces ingestion so the validator
    # catches the mismatch.
    _write_seed(seed_dir, "approach_colorectal.json", _good_universal_seed("approach"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--all", env=env_seed)
    assert r.returncode == 1
    assert "filename does not match" in r.stderr


# ============================================================
# picklist seed — --all mode
# ============================================================


def test_picklist_seed_all_ingests_both_modes(db_with_specialty, tmp_path):
    db, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("procedure", "colorectal"))
    _write_seed(seed_dir, "approach.json", _good_universal_seed("approach"))
    _write_seed(seed_dir, "case_year.json", _good_universal_seed("case_year"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--all", env=env_seed)
    assert r.returncode == 0, r.stderr
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='procedure'")[0]["n"] == 3
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='approach'")[0]["n"] == 2
    assert _query(db, "SELECT COUNT(*) AS n FROM picklist_values WHERE field='case_year'")[0]["n"] == 2


def test_picklist_seed_all_idempotent(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    _write_seed(seed_dir, "procedure_colorectal.json", _good_seed("procedure", "colorectal"))
    _write_seed(seed_dir, "approach.json", _good_universal_seed("approach"))
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r1 = _admin("picklist", "seed", "--all", env=env_seed)
    assert r1.returncode == 0
    r2 = _admin("picklist", "seed", "--all", env=env_seed)
    assert r2.returncode == 0
    # All 5 rows already exist → all skipped.
    assert "inserted=0" in r2.stdout
    assert "skipped=" in r2.stdout


def test_picklist_seed_all_empty_dir_returns_1(db_with_specialty, tmp_path):
    _, env = db_with_specialty
    seed_dir = tmp_path / "picklists"
    seed_dir.mkdir()
    env_seed = {**env, "PIPELINE_PICKLIST_DIR": str(seed_dir)}
    r = _admin("picklist", "seed", "--all", env=env_seed)
    assert r.returncode == 1
    assert "no seed files" in r.stderr


# ============================================================
# picklist seed — mode argument enforcement
# ============================================================


def test_picklist_seed_requires_a_mode(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("picklist", "seed", env=env)
    assert r.returncode != 0
    # argparse error mentions --specialty / --universal / --all
    err = r.stderr
    assert "--specialty" in err or "--universal" in err or "--all" in err


def test_picklist_seed_specialty_and_universal_mutually_exclusive(
    db_with_specialty,
):
    _, env = db_with_specialty
    r = _admin(
        "picklist", "seed", "--specialty", "colorectal", "--universal",
        env=env,
    )
    assert r.returncode != 0
    assert "not allowed with" in r.stderr or "argument" in r.stderr


def test_picklist_seed_universal_and_all_mutually_exclusive(db_with_specialty):
    _, env = db_with_specialty
    r = _admin("picklist", "seed", "--universal", "--all", env=env)
    assert r.returncode != 0


# ============================================================
# metadata.py: data-driven _PICKLIST_SPECIALTIES routing
# ============================================================


def test_metadata_specialties_map_routes_approach_to_universal():
    """Approach must resolve via the universal `approach.json` seed file."""
    from pipeline.commands.metadata import _PICKLIST_SPECIALTIES
    assert _PICKLIST_SPECIALTIES["approach"] is None


def test_metadata_specialties_map_routes_case_year_to_universal():
    from pipeline.commands.metadata import _PICKLIST_SPECIALTIES
    assert _PICKLIST_SPECIALTIES["case_year"] is None


def test_metadata_specialties_map_routes_indication_to_colorectal():
    from pipeline.commands.metadata import _PICKLIST_SPECIALTIES
    assert _PICKLIST_SPECIALTIES["indication"] == "colorectal"


def test_metadata_specialties_map_routes_procedure_to_colorectal():
    from pipeline.commands.metadata import _PICKLIST_SPECIALTIES
    assert _PICKLIST_SPECIALTIES["procedure"] == "colorectal"


# ----- F-021: app.db file mode is 600 (owner-only) -----


def test_init_db_creates_owner_only_file_mode(tmp_path):
    """F-021: init_db tightens umask to 0o077 before sqlite3 creates the
    file so app.db is owner-readable only. Pre-fix it inherited the
    process umask (typically 022 → mode 644), exposing the users table
    to any local account on the DGX."""
    db = tmp_path / "test.db"
    result = _init(db)
    assert result.returncode == 0, result.stderr
    assert db.exists()
    mode = db.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"expected mode 0o600 (owner-only), got 0o{mode:03o}"
    )
