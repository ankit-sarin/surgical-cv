"""Initialize app.db from app/db/schema.sql.

Usage:
    python -m app.db.init_db [--force]

Refuses to overwrite an existing app.db without --force, to prevent accidental
schema reset on a live database. Schema is applied via executescript() under a
single transaction.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.db.connection import connect, db_path


def _schema_path() -> Path:
    return Path(__file__).resolve().parent / "schema.sql"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.db.init_db")
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite app.db if it already exists",
    )
    args = p.parse_args(argv)

    target = db_path()
    if target.exists():
        if not args.force:
            print(
                f"error: {target} already exists. Use --force to overwrite.",
                file=sys.stderr,
            )
            return 1
        target.unlink()

    target.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = _schema_path().read_text()

    conn = connect(target)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    print(f"initialized {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
