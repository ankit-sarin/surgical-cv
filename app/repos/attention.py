"""AttentionItemsRepository — minimal read-side surface for the surgeon
"My Cases" badge derivation.

For the My Cases tab the only question we ask is "does this case have at
least one attention_items row?" — the answer feeds the
:class:`BadgeState.FLAGGED` branch in :func:`app.badges.derive_badge_state`.
The Action Required tab (Brief #3) will extend this surface with full
``list_for_user`` / ``resolve`` / ``dismiss`` methods; it goes here so the
extension is additive rather than a parallel module.

Path resolution mirrors every other SQLite-backed repo: rely on
:func:`app.db.connection.connect` (which honours ``APP_DB_PATH``).
Connection lifecycle uses an explicit ``try/finally`` per the F-030 note
in :mod:`app.db.connection` — ``with connect() as conn:`` only manages
the transaction, not the file descriptor.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from app.db.connection import connect


class AttentionItemsRepository(Protocol):
    def has_attention_for_case_ids(
        self, case_ids: list[str]
    ) -> dict[str, bool]:
        """Map every input case_id to True/False — True iff at least one
        ``attention_items`` row references it. Empty input → empty dict.
        Status filter is intentionally absent — the My Cases badge cares
        about *any* historical flag, not just open ones; resolved items
        still represent reviewer attention the case received."""
        ...


class SqliteAttentionItemsRepository:
    """Production impl. Single ``SELECT DISTINCT case_id FROM attention_items
    WHERE case_id IN (...)`` per call. Connection opened and closed for
    each call (matches :class:`SqlitePicklistRepository` semantics)."""

    def __init__(self, db_path: Path | None = None):
        self._db_path_override = db_path

    def has_attention_for_case_ids(
        self, case_ids: list[str]
    ) -> dict[str, bool]:
        if not case_ids:
            return {}
        all_false = {cid: False for cid in case_ids}
        try:
            conn = connect(self._db_path_override)
        except sqlite3.OperationalError:
            return all_false

        try:
            placeholders = ",".join("?" * len(case_ids))
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT case_id FROM attention_items "
                    f"WHERE case_id IN ({placeholders})",
                    tuple(case_ids),
                ).fetchall()
            except sqlite3.OperationalError:
                # Missing table — uninitialized DB. Same fallback shape
                # the CSV repos use for missing files; never let a
                # configuration miss take the surgeon UI offline.
                return all_false
            flagged = {r["case_id"] for r in rows}
            return {cid: cid in flagged for cid in case_ids}
        finally:
            conn.close()


class InMemoryAttentionItemsRepository:
    """Test fake. Initialize with a set of case_ids that should report True."""

    def __init__(self, flagged_case_ids: set[str] | None = None):
        self._flagged: set[str] = set(flagged_case_ids or set())

    def has_attention_for_case_ids(
        self, case_ids: list[str]
    ) -> dict[str, bool]:
        if not case_ids:
            return {}
        return {cid: cid in self._flagged for cid in case_ids}
