"""SQLite connection helpers for app.db.

``db_path()`` resolves the database path: ``APP_DB_PATH`` env var if set, else
``<project_root>/app/db/app.db``. ``connect()`` opens the database with
``PRAGMA foreign_keys = ON`` and ``row_factory = sqlite3.Row``. ``utcnow()``
returns the canonical ISO 8601 UTC timestamp string used as ``created_at`` /
``*_at`` values across the schema.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB_SUBPATH = ("app", "db", "app.db")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def db_path() -> Path:
    env = os.environ.get("APP_DB_PATH")
    if env:
        return Path(env)
    return _project_root().joinpath(*_DEFAULT_DB_SUBPATH)


def connect(path: Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = db_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
