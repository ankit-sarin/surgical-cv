"""Admin CLI for managing app.db.

Subcommands:
    user      add | update | deactivate | list | show
    specialty add | list
    picklist  add | list | deactivate | seed

stdlib + argparse only. No ORM, no third-party CLI libraries.

Role invariant (surgeon → folder_slug + specialty; admin → neither) is enforced
twice: in Python before insert for friendly errors, and by a table-level CHECK
in schema.sql as a safety net. ``user update`` rejects the three invariant
fields (role / folder_slug / specialty) without touching the DB.

``picklist seed`` reads ``*_<specialty>.json`` from ``$PIPELINE_PICKLIST_DIR``
(or ``app/db/seeds/picklists/`` by default — same convention as the
``pipeline.picklists`` loader). Each file's ``field`` and ``specialty`` JSON
tags must match the filename + CLI args. INSERT OR IGNORE makes re-runs safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from app.db.connection import connect, utcnow


_USER_IMMUTABLE_FIELDS = ("role", "folder_slug", "specialty", "username", "created_at")
_USER_EDITABLE_FIELDS = ("display_name", "email", "active", "notes", "last_login_at")


# ----- formatting helpers -----


def _fmt_bool(v) -> str:
    return "yes" if v else "no"


def _dash(v) -> str:
    return "-" if v in (None, "") else str(v)


# ----- user subcommands -----


def _user_add(conn: sqlite3.Connection, args) -> int:
    if args.role == "surgeon":
        if not args.folder_slug or not args.specialty:
            print(
                "error: surgeons require --folder-slug and --specialty",
                file=sys.stderr,
            )
            return 1
    else:  # admin
        if args.folder_slug or args.specialty:
            print(
                "error: admins must not have --folder-slug or --specialty",
                file=sys.stderr,
            )
            return 1

    if args.specialty is not None:
        row = conn.execute(
            "SELECT 1 FROM specialties WHERE specialty_code = ?",
            (args.specialty,),
        ).fetchone()
        if row is None:
            print(
                f"error: specialty {args.specialty!r} does not exist",
                file=sys.stderr,
            )
            return 1

    try:
        conn.execute(
            "INSERT INTO users "
            "(username, role, folder_slug, specialty, display_name, email, "
            " active, created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                args.username,
                args.role,
                args.folder_slug,
                args.specialty,
                args.display_name,
                args.email,
                utcnow(),
                args.notes,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"added user {args.username!r} ({args.role})")
    return 0


def _user_update(conn: sqlite3.Connection, args) -> int:
    field = args.field
    if field in _USER_IMMUTABLE_FIELDS:
        print(
            f"error: field {field!r} is immutable; "
            "role/folder_slug/specialty cannot change after creation",
            file=sys.stderr,
        )
        return 1
    if field not in _USER_EDITABLE_FIELDS:
        print(
            f"error: unknown field {field!r}; editable fields: "
            f"{', '.join(_USER_EDITABLE_FIELDS)}",
            file=sys.stderr,
        )
        return 1

    row = conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (args.username,)
    ).fetchone()
    if row is None:
        print(f"error: user {args.username!r} not found", file=sys.stderr)
        return 1

    write_value = args.value
    if field == "active":
        if args.value not in ("0", "1"):
            print("error: active must be 0 or 1", file=sys.stderr)
            return 1
        write_value = int(args.value)

    conn.execute(
        f"UPDATE users SET {field} = ? WHERE username = ?",
        (write_value, args.username),
    )
    conn.commit()
    print(f"updated {args.username!r}: {field} = {args.value!r}")
    return 0


def _user_deactivate(conn: sqlite3.Connection, args) -> int:
    row = conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (args.username,)
    ).fetchone()
    if row is None:
        print(f"error: user {args.username!r} not found", file=sys.stderr)
        return 1
    conn.execute(
        "UPDATE users SET active = 0 WHERE username = ?", (args.username,)
    )
    conn.commit()
    print(f"deactivated {args.username!r}")
    return 0


def _user_list(conn: sqlite3.Connection, args) -> int:
    sql = (
        "SELECT username, role, folder_slug, specialty, display_name, active "
        "FROM users"
    )
    conditions: list[str] = []
    params: list = []
    if args.role:
        conditions.append("role = ?")
        params.append(args.role)
    if args.specialty:
        conditions.append("specialty = ?")
        params.append(args.specialty)
    if not args.inactive:
        conditions.append("active = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY role DESC, username"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("(no users)")
        return 0
    print(
        f"{'USERNAME':<14} {'ROLE':<8} {'FOLDER_SLUG':<12} "
        f"{'SPECIALTY':<12} {'ACTIVE':<6} {'DISPLAY_NAME'}"
    )
    for r in rows:
        print(
            f"{r['username']:<14} {r['role']:<8} "
            f"{_dash(r['folder_slug']):<12} "
            f"{_dash(r['specialty']):<12} "
            f"{_fmt_bool(r['active']):<6} {_dash(r['display_name'])}"
        )
    return 0


def _user_show(conn: sqlite3.Connection, args) -> int:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (args.username,)
    ).fetchone()
    if row is None:
        print(f"error: user {args.username!r} not found", file=sys.stderr)
        return 1
    fields = (
        "username",
        "role",
        "folder_slug",
        "specialty",
        "display_name",
        "email",
        "active",
        "created_at",
        "last_login_at",
        "notes",
    )
    for f in fields:
        v = row[f]
        if f == "active":
            v = _fmt_bool(v)
        else:
            v = _dash(v)
        print(f"{f}: {v}")
    return 0


# ----- specialty subcommands -----


def _specialty_add(conn: sqlite3.Connection, args) -> int:
    try:
        conn.execute(
            "INSERT INTO specialties "
            "(specialty_code, display_name, active, created_at) "
            "VALUES (?, ?, 1, ?)",
            (args.code, args.display_name, utcnow()),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"added specialty {args.code!r}")
    return 0


def _specialty_list(conn: sqlite3.Connection, args) -> int:
    sql = "SELECT specialty_code, display_name, active FROM specialties"
    if not args.inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY specialty_code"
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("(no specialties)")
        return 0
    print(f"{'CODE':<14} {'DISPLAY_NAME':<28} {'ACTIVE'}")
    for r in rows:
        print(
            f"{r['specialty_code']:<14} {r['display_name']:<28} "
            f"{_fmt_bool(r['active'])}"
        )
    return 0


# ----- picklist subcommands -----


def _picklist_add(conn: sqlite3.Connection, args) -> int:
    if args.specialty is not None:
        row = conn.execute(
            "SELECT 1 FROM specialties WHERE specialty_code = ?",
            (args.specialty,),
        ).fetchone()
        if row is None:
            print(
                f"error: specialty {args.specialty!r} does not exist",
                file=sys.stderr,
            )
            return 1
    display_label = args.display_label or args.value
    try:
        conn.execute(
            "INSERT INTO picklist_values "
            "(field, value, display_label, sort_order, active, specialty, "
            " created_at, created_by) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (
                args.field,
                args.value,
                display_label,
                args.sort_order,
                args.specialty,
                utcnow(),
                args.created_by,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"added picklist value: field={args.field!r} value={args.value!r}")
    return 0


def _picklist_list(conn: sqlite3.Connection, args) -> int:
    sql = (
        "SELECT field, value, display_label, sort_order, active, specialty "
        "FROM picklist_values"
    )
    conditions: list[str] = []
    params: list = []
    if args.field:
        conditions.append("field = ?")
        params.append(args.field)
    if args.specialty:
        conditions.append("specialty = ?")
        params.append(args.specialty)
    if not args.inactive:
        conditions.append("active = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY field, sort_order, value"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("(no picklist values)")
        return 0
    print(
        f"{'FIELD':<14} {'SORT':>5}  {'VALUE':<54} "
        f"{'SPECIALTY':<12} {'ACTIVE'}"
    )
    for r in rows:
        print(
            f"{r['field']:<14} {r['sort_order']:>5}  {r['value']:<54} "
            f"{_dash(r['specialty']):<12} "
            f"{_fmt_bool(r['active'])}"
        )
    return 0


def _picklist_deactivate(conn: sqlite3.Connection, args) -> int:
    row = conn.execute(
        "SELECT 1 FROM picklist_values WHERE field = ? AND value = ?",
        (args.field, args.value),
    ).fetchone()
    if row is None:
        print(
            f"error: picklist value (field={args.field!r}, value={args.value!r}) "
            "not found",
            file=sys.stderr,
        )
        return 1
    conn.execute(
        "UPDATE picklist_values SET active = 0 WHERE field = ? AND value = ?",
        (args.field, args.value),
    )
    conn.commit()
    print(
        f"deactivated picklist value: field={args.field!r} value={args.value!r}"
    )
    return 0


def _seed_dir() -> Path:
    env = os.environ.get("PIPELINE_PICKLIST_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "seeds" / "picklists"


def _picklist_seed(conn: sqlite3.Connection, args) -> int:
    specialty = args.specialty
    row = conn.execute(
        "SELECT 1 FROM specialties WHERE specialty_code = ?", (specialty,)
    ).fetchone()
    if row is None:
        print(
            f"error: specialty {specialty!r} does not exist", file=sys.stderr
        )
        return 1

    seed_dir = _seed_dir()
    if not seed_dir.exists():
        print(f"error: seed dir not found: {seed_dir}", file=sys.stderr)
        return 1

    if args.field:
        candidates = [seed_dir / f"{args.field}_{specialty}.json"]
        if not candidates[0].exists():
            print(
                f"error: seed file not found: {candidates[0]}", file=sys.stderr
            )
            return 1
    else:
        candidates = sorted(seed_dir.glob(f"*_{specialty}.json"))
        if not candidates:
            print(
                f"error: no seed files matching *_{specialty}.json in {seed_dir}",
                file=sys.stderr,
            )
            return 1

    total_inserted = 0
    total_skipped = 0
    files_processed = 0
    suffix = f"_{specialty}"

    for path in candidates:
        stem = path.stem
        if not stem.endswith(suffix):
            print(
                f"error: seed file {path.name} does not match "
                f"*_{specialty}.json pattern",
                file=sys.stderr,
            )
            return 1
        filename_field = stem[: -len(suffix)]

        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: failed to read {path}: {e}", file=sys.stderr)
            return 1

        if not isinstance(data, dict):
            print(
                f"error: {path}: expected object at top level", file=sys.stderr
            )
            return 1
        json_field = data.get("field")
        json_specialty = data.get("specialty")
        if json_field != filename_field:
            print(
                f"error: {path.name}: JSON field={json_field!r} does not "
                f"match filename field {filename_field!r}",
                file=sys.stderr,
            )
            return 1
        if json_specialty != specialty:
            print(
                f"error: {path.name}: JSON specialty={json_specialty!r} does "
                f"not match --specialty {specialty!r}",
                file=sys.stderr,
            )
            return 1
        values = data.get("values")
        if not isinstance(values, list):
            print(
                f"error: {path.name}: 'values' must be a list",
                file=sys.stderr,
            )
            return 1

        inserted = 0
        skipped = 0
        ts = utcnow()
        for i, item in enumerate(values):
            if not isinstance(item, dict):
                print(
                    f"error: {path.name}: values[{i}] must be an object",
                    file=sys.stderr,
                )
                return 1
            for key in ("value", "display_label", "sort_order"):
                if key not in item:
                    print(
                        f"error: {path.name}: values[{i}] missing {key!r}",
                        file=sys.stderr,
                    )
                    return 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO picklist_values "
                "(field, value, display_label, sort_order, active, specialty, "
                " created_at, created_by) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    filename_field,
                    item["value"],
                    item["display_label"],
                    item["sort_order"],
                    specialty,
                    ts,
                    args.created_by,
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
        print(f"{path.name}: inserted={inserted} skipped={skipped}")
        total_inserted += inserted
        total_skipped += skipped
        files_processed += 1

    print(
        f"seed complete: {files_processed} file(s), "
        f"{total_inserted} inserted, {total_skipped} skipped"
    )
    return 0


# ----- argparse plumbing -----


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.db.admin_cli")
    sub = p.add_subparsers(dest="entity", required=True)

    # user
    user_p = sub.add_parser("user")
    user_sub = user_p.add_subparsers(dest="action", required=True)

    add_p = user_sub.add_parser("add")
    add_p.add_argument("username")
    add_p.add_argument("--role", required=True, choices=("surgeon", "admin"))
    add_p.add_argument("--folder-slug", dest="folder_slug")
    add_p.add_argument("--specialty")
    add_p.add_argument("--display-name", dest="display_name")
    add_p.add_argument("--email")
    add_p.add_argument("--notes")

    upd_p = user_sub.add_parser("update")
    upd_p.add_argument("username")
    upd_p.add_argument("field")
    upd_p.add_argument("value")

    deact_p = user_sub.add_parser("deactivate")
    deact_p.add_argument("username")

    list_p = user_sub.add_parser("list")
    list_p.add_argument("--role", choices=("surgeon", "admin"))
    list_p.add_argument("--specialty")
    list_p.add_argument(
        "--inactive", action="store_true", help="include inactive users"
    )

    show_p = user_sub.add_parser("show")
    show_p.add_argument("username")

    # specialty
    spec_p = sub.add_parser("specialty")
    spec_sub = spec_p.add_subparsers(dest="action", required=True)

    sadd = spec_sub.add_parser("add")
    sadd.add_argument("code")
    sadd.add_argument("--display-name", dest="display_name", required=True)

    slist = spec_sub.add_parser("list")
    slist.add_argument("--inactive", action="store_true")

    # picklist
    pl_p = sub.add_parser("picklist")
    pl_sub = pl_p.add_subparsers(dest="action", required=True)

    padd = pl_sub.add_parser("add")
    padd.add_argument("field")
    padd.add_argument("value")
    padd.add_argument("--specialty")
    padd.add_argument("--display-label", dest="display_label")
    padd.add_argument("--sort-order", dest="sort_order", type=int, default=0)
    padd.add_argument("--created-by", dest="created_by")

    plist = pl_sub.add_parser("list")
    plist.add_argument("field", nargs="?")
    plist.add_argument("--specialty")
    plist.add_argument("--inactive", action="store_true")

    pdeact = pl_sub.add_parser("deactivate")
    pdeact.add_argument("field")
    pdeact.add_argument("value")

    pseed = pl_sub.add_parser("seed")
    pseed.add_argument("--specialty", required=True)
    pseed.add_argument("--field")
    pseed.add_argument("--created-by", dest="created_by")

    return p


_DISPATCH = {
    ("user", "add"): _user_add,
    ("user", "update"): _user_update,
    ("user", "deactivate"): _user_deactivate,
    ("user", "list"): _user_list,
    ("user", "show"): _user_show,
    ("specialty", "add"): _specialty_add,
    ("specialty", "list"): _specialty_list,
    ("picklist", "add"): _picklist_add,
    ("picklist", "list"): _picklist_list,
    ("picklist", "deactivate"): _picklist_deactivate,
    ("picklist", "seed"): _picklist_seed,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH[(args.entity, args.action)]
    conn = connect()
    try:
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
