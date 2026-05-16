"""AttentionItemsRepository — read + state-transition surface for the
surgeon Action Required tab and the My Cases badge derivation.

Three concerns live here:

  - :meth:`has_attention_for_case_ids` — set-membership lookup driving the
    Flagged badge in the My Cases tab (Brief #2).
  - :meth:`list_for_user` — surgeon-scoped list used by the Action
    Required tab (Brief #3).
  - :meth:`resolve` / :meth:`dismiss` — state transitions with central-
    ized validation gates and an audit-row write per action.

All four surface methods live on the same Protocol so a future migration
(e.g. moving attention items into a dedicated table cluster) only
touches this module.

Path resolution mirrors every other SQLite-backed repo: rely on
:func:`app.db.connection.connect` (which honours ``APP_DB_PATH``).
Connection lifecycle uses an explicit ``try/finally`` per the F-030 note
in :mod:`app.db.connection`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Protocol

from app.attention_actions import SURGEON_ACTION_BY_TYPE
from app.db.connection import connect, utcnow
from app.exceptions import ScopeViolationError


# Canonical reason sentinel for surgeon-initiated state transitions.
# admin_audit.reason is NOT NULL per schema; the column name is
# misleading for surgeon actors (v23 plan note: rename to actor_username
# + actor_role; out of scope for this brief). The sentinel keeps the
# audit row honest about the actor type until then.
SURGEON_AUDIT_REASON = "(surgeon-initiated)"


# Action verbs allowed on the audit log. Keep the strings short and
# greppable; admins will scan the table by these prefixes.
_AUDIT_ACTION_RESOLVE = "attention.resolve"
_AUDIT_ACTION_DISMISS = "attention.dismiss"

# Status discriminators in attention_items. ``"open"`` is the only state
# that's actionable; ``"resolved"`` and ``"dismissed"`` are terminal.
_STATUS_OPEN = "open"
_STATUS_RESOLVED = "resolved"
_STATUS_DISMISSED = "dismissed"


# ----- exception classes -----


class AttentionRepoError(Exception):
    """Base for repo-layer attention errors. Carries item_id so callers
    (Gradio handler, tests) can attribute failures to a specific row."""

    def __init__(self, item_id: int, message: str):
        self.item_id = item_id
        super().__init__(message)


class AttentionItemNotFoundError(AttentionRepoError):
    """No row with the given id."""


class AttentionItemAlreadyClosedError(AttentionRepoError):
    """Item is no longer in status='open' — typically a double-click or a
    stale-tab race where another tab/admin already actioned the row."""


class AttentionItemActionMismatchError(AttentionRepoError):
    """The requested action doesn't match the dispatch table for this
    item's type. Catches a UI bypass (curl, malformed event) attempting
    to e.g. dismiss a pipeline_failure that should require resolve."""


# Backwards-compat alias for the brief's documented name. Keep both so
# external callers / future refactors can pick whichever reads cleaner.
AttentionItemNotResolvableError = AttentionItemActionMismatchError


# ----- read shape -----


@dataclass(frozen=True)
class AttentionItem:
    """Surgeon-visible read shape. Mirrors the SQLite row but coerces
    types to Python primitives so callers don't see sqlite3.Row objects
    leaking through the repo boundary."""
    id: int
    type: str
    case_id: str | None
    affected_user: str
    severity: str
    details: str | None
    status: str
    created_at: str
    created_by: str
    resolved_at: str | None
    resolved_by: str | None
    resolution_note: str | None

    @classmethod
    def from_row(cls, row) -> "AttentionItem":
        return cls(
            id=int(row["id"]),
            type=row["type"],
            case_id=row["case_id"],
            affected_user=row["affected_user"],
            severity=row["severity"],
            details=row["details"],
            status=row["status"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            resolved_at=row["resolved_at"],
            resolved_by=row["resolved_by"],
            resolution_note=row["resolution_note"],
        )


# ----- protocol -----


class AttentionItemsRepository(Protocol):
    def has_attention_for_case_ids(
        self, case_ids: list[str]
    ) -> dict[str, bool]:
        """My Cases badge derivation. See :class:`BadgeState.FLAGGED`."""
        ...

    def list_for_user(
        self,
        username: str,
        status: Literal["open", "resolved", "dismissed"] = "open",
    ) -> list[AttentionItem]:
        """Surgeon-scoped list, newest first by ``created_at`` then by
        ``id`` DESC for tiebreak. Default ``status="open"`` covers the
        Action Required tab; pass ``"resolved"``/``"dismissed"`` for
        future history views."""
        ...

    def resolve(self, item_id: int, by: str) -> AttentionItem:
        """Transition open → resolved. Validates existence, status, and
        scope; writes an audit row. Returns the updated row."""
        ...

    def dismiss(self, item_id: int, by: str) -> AttentionItem:
        """Transition open → dismissed. Same validation + audit shape as
        :meth:`resolve`."""
        ...

    def count_actions_today(
        self, username: str, today_start_iso: str
    ) -> int:
        """Counter-strip support for the Action Required tab: how many
        attention.{resolve,dismiss} audit rows did ``username`` write
        since ``today_start_iso`` (UTC midnight in v1)."""
        ...


# ----- shared validation helpers -----


def _validate_action_for_type(
    item_id: int, item_type: str, action: Literal["resolve", "dismiss"]
) -> None:
    """Confirm the requested action matches SURGEON_ACTION_BY_TYPE.
    Raised before any state mutation so the audit row never lands on a
    rejected transition."""
    expected = SURGEON_ACTION_BY_TYPE.get(item_type)
    if expected is None:
        # Unknown type — no action allowed from the surgeon UI. The card
        # renders read-only in this case; reaching this branch implies a
        # UI bypass (direct curl, stale event).
        raise AttentionItemActionMismatchError(
            item_id,
            f"item type {item_type!r} has no surgeon action mapped; "
            f"action {action!r} rejected",
        )
    if expected != action:
        raise AttentionItemActionMismatchError(
            item_id,
            f"item type {item_type!r} expects action {expected!r}, "
            f"got {action!r}",
        )


def _scope_check(item: AttentionItem, by: str, action: str) -> None:
    """Cross-silo guard: the actor must own the item. Raises
    ScopeViolationError so the central handler logs + 403s. Out-of-band
    by design — surgeons can never touch each other's items even if a
    UI bug surfaces a foreign id."""
    if item.affected_user != by:
        raise ScopeViolationError(
            resource=f"attention_item:{item.id}",
            action=f"attention.{action}",
            scope_at_time=f"surgeon:{by}",
        )


# ----- SQLite implementation -----


class SqliteAttentionItemsRepository:
    """Production impl. Each method opens + closes its own connection,
    matching the F-030 lifecycle convention used by the other repos."""

    def __init__(self, db_path: Path | None = None):
        self._db_path_override = db_path

    def _connect(self) -> sqlite3.Connection | None:
        try:
            return connect(self._db_path_override)
        except sqlite3.OperationalError:
            return None

    def has_attention_for_case_ids(
        self, case_ids: list[str]
    ) -> dict[str, bool]:
        if not case_ids:
            return {}
        all_false = {cid: False for cid in case_ids}
        conn = self._connect()
        if conn is None:
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
                return all_false
            flagged = {r["case_id"] for r in rows}
            return {cid: cid in flagged for cid in case_ids}
        finally:
            conn.close()

    def list_for_user(
        self,
        username: str,
        status: Literal["open", "resolved", "dismissed"] = "open",
    ) -> list[AttentionItem]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            try:
                rows = conn.execute(
                    "SELECT * FROM attention_items "
                    "WHERE affected_user = ? AND status = ? "
                    "ORDER BY created_at DESC, id DESC",
                    (username, status),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [AttentionItem.from_row(r) for r in rows]
        finally:
            conn.close()

    def count_actions_today(
        self, username: str, today_start_iso: str
    ) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM admin_audit "
                    "WHERE admin_username = ? "
                    "  AND action IN (?, ?) "
                    "  AND created_at >= ?",
                    (
                        username,
                        _AUDIT_ACTION_RESOLVE,
                        _AUDIT_ACTION_DISMISS,
                        today_start_iso,
                    ),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def resolve(self, item_id: int, by: str) -> AttentionItem:
        return self._transition(
            item_id, by, action="resolve", new_status=_STATUS_RESOLVED,
            audit_action=_AUDIT_ACTION_RESOLVE,
        )

    def dismiss(self, item_id: int, by: str) -> AttentionItem:
        return self._transition(
            item_id, by, action="dismiss", new_status=_STATUS_DISMISSED,
            audit_action=_AUDIT_ACTION_DISMISS,
        )

    def _transition(
        self,
        item_id: int,
        by: str,
        *,
        action: Literal["resolve", "dismiss"],
        new_status: str,
        audit_action: str,
    ) -> AttentionItem:
        """Atomic state transition + audit-row write. Validation gates
        run BEFORE the write so a rejected action never leaves an audit
        row behind."""
        conn = connect(self._db_path_override)
        try:
            # 1. Existence + load.
            row = conn.execute(
                "SELECT * FROM attention_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise AttentionItemNotFoundError(
                    item_id, f"no attention_items row with id={item_id}"
                )
            item = AttentionItem.from_row(row)

            # 2. Status gate — only open items are actionable.
            if item.status != _STATUS_OPEN:
                raise AttentionItemAlreadyClosedError(
                    item_id,
                    f"attention_items id={item_id} has status "
                    f"{item.status!r}; only 'open' items are actionable",
                )

            # 3. Type/action match (raises before any DB mutation).
            _validate_action_for_type(item_id, item.type, action)

            # 4. Scope check — actor must own the item.
            _scope_check(item, by, action)

            # 5. Mutate + audit, in one transaction.
            now = utcnow()
            conn.execute(
                "UPDATE attention_items "
                "SET status = ?, resolved_at = ?, resolved_by = ? "
                "WHERE id = ?",
                (new_status, now, by, item_id),
            )
            conn.execute(
                "INSERT INTO admin_audit "
                "(admin_username, action, target_kind, target_id, "
                " before_value, after_value, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    by,
                    audit_action,
                    "attention_item",
                    str(item_id),
                    _STATUS_OPEN,
                    new_status,
                    SURGEON_AUDIT_REASON,
                    now,
                ),
            )
            conn.commit()

            updated = conn.execute(
                "SELECT * FROM attention_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            return AttentionItem.from_row(updated)
        finally:
            conn.close()


# ----- in-memory implementation -----


class InMemoryAttentionItemsRepository:
    """Test fake. Initialize with either a flat set of flagged case ids
    (legacy Brief #2 shape, kept for backward compatibility) OR a list
    of full :class:`AttentionItem` rows. The full-row form is required
    for Brief #3 surfaces (list_for_user, resolve, dismiss)."""

    def __init__(
        self,
        flagged_case_ids: set[str] | None = None,
        items: Iterable[AttentionItem] | None = None,
    ):
        self._flagged: set[str] = set(flagged_case_ids or set())
        self._items: dict[int, AttentionItem] = {}
        self._audit: list[dict] = []
        for it in items or []:
            self._items[it.id] = it
            if it.case_id is not None and it.status == _STATUS_OPEN:
                self._flagged.add(it.case_id)

    # ----- introspection helpers (test surface, not part of Protocol) -----

    @property
    def audit(self) -> list[dict]:
        """Audit rows written by resolve/dismiss, in insert order."""
        return list(self._audit)

    # ----- repo protocol -----

    def has_attention_for_case_ids(
        self, case_ids: list[str]
    ) -> dict[str, bool]:
        if not case_ids:
            return {}
        # Treat any case with a row in self._items (regardless of status)
        # as flagged, matching the SQLite behavior. Plus any legacy
        # init-time flagged ids that don't have a corresponding item.
        from_items = {
            it.case_id for it in self._items.values() if it.case_id
        }
        flagged = self._flagged | from_items
        return {cid: cid in flagged for cid in case_ids}

    def list_for_user(
        self,
        username: str,
        status: Literal["open", "resolved", "dismissed"] = "open",
    ) -> list[AttentionItem]:
        matching = [
            it for it in self._items.values()
            if it.affected_user == username and it.status == status
        ]
        # Newest first by created_at, tiebreak by id DESC — same key as
        # the SQLite ORDER BY clause.
        matching.sort(key=lambda it: (it.created_at, it.id), reverse=True)
        return matching

    def count_actions_today(
        self, username: str, today_start_iso: str
    ) -> int:
        return sum(
            1 for row in self._audit
            if row["admin_username"] == username
            and row["action"] in (_AUDIT_ACTION_RESOLVE, _AUDIT_ACTION_DISMISS)
            and row["created_at"] >= today_start_iso
        )

    def resolve(self, item_id: int, by: str) -> AttentionItem:
        return self._transition(
            item_id, by, action="resolve", new_status=_STATUS_RESOLVED,
            audit_action=_AUDIT_ACTION_RESOLVE,
        )

    def dismiss(self, item_id: int, by: str) -> AttentionItem:
        return self._transition(
            item_id, by, action="dismiss", new_status=_STATUS_DISMISSED,
            audit_action=_AUDIT_ACTION_DISMISS,
        )

    def _transition(
        self,
        item_id: int,
        by: str,
        *,
        action: Literal["resolve", "dismiss"],
        new_status: str,
        audit_action: str,
    ) -> AttentionItem:
        item = self._items.get(item_id)
        if item is None:
            raise AttentionItemNotFoundError(
                item_id, f"no attention item with id={item_id}"
            )
        if item.status != _STATUS_OPEN:
            raise AttentionItemAlreadyClosedError(
                item_id,
                f"attention item id={item_id} has status {item.status!r}; "
                f"only 'open' items are actionable",
            )
        _validate_action_for_type(item_id, item.type, action)
        _scope_check(item, by, action)

        now = utcnow()
        updated = AttentionItem(
            id=item.id,
            type=item.type,
            case_id=item.case_id,
            affected_user=item.affected_user,
            severity=item.severity,
            details=item.details,
            status=new_status,
            created_at=item.created_at,
            created_by=item.created_by,
            resolved_at=now,
            resolved_by=by,
            resolution_note=item.resolution_note,
        )
        self._items[item_id] = updated
        self._audit.append({
            "admin_username": by,
            "action": audit_action,
            "target_kind": "attention_item",
            "target_id": str(item_id),
            "before_value": _STATUS_OPEN,
            "after_value": new_status,
            "reason": SURGEON_AUDIT_REASON,
            "created_at": now,
        })
        return updated
