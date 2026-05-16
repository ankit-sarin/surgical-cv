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
# admin_audit.reason is NOT NULL per schema. Brief #4 renamed
# admin_username → actor_username and added actor_role; the surgeon
# self-service writes record actor_role='surgeon' and use this
# sentinel for reason. Admin writes pass the operator-supplied
# free-text reason instead.
SURGEON_AUDIT_REASON = "(surgeon-initiated)"

# Brief #4: minimum reason length enforced at the repo boundary so a
# UI-bypass call (curl, test fake, mis-firing handler) can't write a
# blank-reason admin_audit row. Mirrored client-side in the Gradio
# tab for inline UX, but the server check is the authoritative gate.
ADMIN_REASON_MIN_LENGTH = 10


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
    # Brief #3.5b: advances on every ``upsert_by_case_and_type``;
    # equals ``created_at`` for rows inserted via the plain
    # ``write_attention_item`` path. Default keeps in-memory test
    # construction unbreaking when callers don't care about the
    # value — SQLite rows always populate it via ``from_row``.
    updated_at: str = ""

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
            updated_at=row["updated_at"],
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

    def list_all(
        self,
        status: Literal["open", "resolved", "dismissed"] = "open",
    ) -> list[AttentionItem]:
        """Brief #4: unscoped cross-silo list for the admin Action
        Required tab. Same ordering as :meth:`list_for_user` (newest
        first by ``created_at``, tiebreak ``id`` DESC). No role check
        inside the repo — auth lives at the admin mount."""
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

    def upsert_by_case_and_type(
        self,
        *,
        case_id: str,
        item_type: str,
        affected_user: str,
        severity: str,
        details: str,
    ) -> AttentionItem:
        """Brief #3.5b: per-case rollup. If an open ``phi_redacted`` row
        exists for ``case_id``, update its ``details``, ``severity``, and
        ``updated_at`` in place; otherwise insert a fresh open row.
        Single SQL transaction — never read-then-create at the app layer.
        The schema-level partial unique index
        (``idx_attention_phi_redacted_case_uniq``) is the conflict target;
        ``item_type`` must be ``'phi_redacted'`` for now (other types use
        the plain INSERT path in :func:`write_attention_item`)."""
        ...

    def admin_resolve(
        self,
        item_id: int,
        by_admin: str,
        *,
        reason: str,
        on_behalf_of: str | None,
    ) -> AttentionItem:
        """Brief #4: admin-side resolve. Bypasses the surgeon-side
        action-type validation and scope check (admin is the override
        path). Writes one ``admin_audit`` row with ``actor_role='admin'``
        and the surgeon-side ``resolved_on_behalf_of`` column populated
        when the admin is acting on a surgeon's behalf."""
        ...

    def admin_dismiss(
        self,
        item_id: int,
        by_admin: str,
        *,
        reason: str,
    ) -> AttentionItem:
        """Brief #4: admin-side dismiss. Same shape as
        :meth:`admin_resolve` minus the on-behalf-of column (dismiss
        is "this never required surgeon action" — no surgeon to credit)."""
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


def _validate_admin_reason(item_id: int, reason: str) -> str:
    """Server-side gate for admin dismiss/resolve. The Gradio UI does an
    inline check, but never trust the client. Returns the stripped
    reason so callers can persist a normalized value."""
    if reason is None:
        raise ValueError(
            f"admin action on attention item {item_id} requires a reason "
            f"(got None)"
        )
    stripped = reason.strip()
    if len(stripped) < ADMIN_REASON_MIN_LENGTH:
        raise ValueError(
            f"admin action on attention item {item_id} requires a reason "
            f"of at least {ADMIN_REASON_MIN_LENGTH} characters "
            f"(got {len(stripped)})"
        )
    return stripped


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

    def list_all(
        self,
        status: Literal["open", "resolved", "dismissed"] = "open",
    ) -> list[AttentionItem]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            try:
                rows = conn.execute(
                    "SELECT * FROM attention_items "
                    "WHERE status = ? "
                    "ORDER BY created_at DESC, id DESC",
                    (status,),
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
                    "WHERE actor_username = ? "
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

    def upsert_by_case_and_type(
        self,
        *,
        case_id: str,
        item_type: str,
        affected_user: str,
        severity: str,
        details: str,
    ) -> AttentionItem:
        # The partial unique index this upsert targets is phi_redacted-only.
        # Other item types use the plain INSERT path in
        # ``write_attention_item`` — they'd produce duplicate rows here
        # because the ON CONFLICT clause wouldn't match their inserts.
        if item_type != "phi_redacted":
            raise ValueError(
                f"upsert_by_case_and_type only supports type='phi_redacted'; "
                f"got {item_type!r}"
            )
        now = utcnow()
        # ``ON CONFLICT (case_id) WHERE ...`` must mirror the partial
        # index's WHERE clause exactly so SQLite can identify the
        # target index. ``excluded`` references the row that would
        # have been inserted; we update only the surgeon-visible
        # payload + the bookkeeping timestamp, never created_at /
        # created_by / status / id (those stay frozen at first emit).
        conn = connect(self._db_path_override)
        try:
            row = conn.execute(
                "INSERT INTO attention_items "
                "(type, case_id, affected_user, severity, details, "
                " created_at, created_by, updated_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open') "
                "ON CONFLICT (case_id) "
                "  WHERE case_id IS NOT NULL AND type = 'phi_redacted' "
                "DO UPDATE SET "
                "  details = excluded.details, "
                "  severity = excluded.severity, "
                "  updated_at = excluded.updated_at "
                "RETURNING *",
                (
                    item_type,
                    case_id,
                    affected_user,
                    severity,
                    details,
                    now,
                    "system_worker",
                    now,
                ),
            ).fetchone()
            conn.commit()
            return AttentionItem.from_row(row)
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

    def admin_resolve(
        self,
        item_id: int,
        by_admin: str,
        *,
        reason: str,
        on_behalf_of: str | None,
    ) -> AttentionItem:
        return self._admin_transition(
            item_id, by_admin,
            new_status=_STATUS_RESOLVED,
            audit_action=_AUDIT_ACTION_RESOLVE,
            reason=reason,
            on_behalf_of=on_behalf_of,
        )

    def admin_dismiss(
        self,
        item_id: int,
        by_admin: str,
        *,
        reason: str,
    ) -> AttentionItem:
        return self._admin_transition(
            item_id, by_admin,
            new_status=_STATUS_DISMISSED,
            audit_action=_AUDIT_ACTION_DISMISS,
            reason=reason,
            on_behalf_of=None,
        )

    def _admin_transition(
        self,
        item_id: int,
        by_admin: str,
        *,
        new_status: str,
        audit_action: str,
        reason: str,
        on_behalf_of: str | None,
    ) -> AttentionItem:
        """Admin override path. Skips surgeon-side validation
        (action-type, scope) — the admin queue is the override
        surface for both. Still gates on existence + open status so
        a double-click race can't double-write audit rows."""
        stripped_reason = _validate_admin_reason(item_id, reason)
        conn = connect(self._db_path_override)
        try:
            row = conn.execute(
                "SELECT * FROM attention_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise AttentionItemNotFoundError(
                    item_id, f"no attention_items row with id={item_id}"
                )
            item = AttentionItem.from_row(row)
            if item.status != _STATUS_OPEN:
                raise AttentionItemAlreadyClosedError(
                    item_id,
                    f"attention_items id={item_id} has status "
                    f"{item.status!r}; only 'open' items are actionable",
                )

            now = utcnow()
            conn.execute(
                "UPDATE attention_items "
                "SET status = ?, resolved_at = ?, resolved_by = ? "
                "WHERE id = ?",
                (new_status, now, by_admin, item_id),
            )
            conn.execute(
                "INSERT INTO admin_audit "
                "(actor_username, actor_role, action, target_kind, target_id, "
                " before_value, after_value, reason, "
                " resolved_on_behalf_of, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    by_admin,
                    "admin",
                    audit_action,
                    "attention_item",
                    str(item_id),
                    _STATUS_OPEN,
                    new_status,
                    stripped_reason,
                    on_behalf_of,
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
                "(actor_username, actor_role, action, target_kind, target_id, "
                " before_value, after_value, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    by,
                    "surgeon",
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

    def list_all(
        self,
        status: Literal["open", "resolved", "dismissed"] = "open",
    ) -> list[AttentionItem]:
        matching = [
            it for it in self._items.values() if it.status == status
        ]
        matching.sort(key=lambda it: (it.created_at, it.id), reverse=True)
        return matching

    def count_actions_today(
        self, username: str, today_start_iso: str
    ) -> int:
        return sum(
            1 for row in self._audit
            if row["actor_username"] == username
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

    def admin_resolve(
        self,
        item_id: int,
        by_admin: str,
        *,
        reason: str,
        on_behalf_of: str | None,
    ) -> AttentionItem:
        return self._admin_transition(
            item_id, by_admin,
            new_status=_STATUS_RESOLVED,
            audit_action=_AUDIT_ACTION_RESOLVE,
            reason=reason,
            on_behalf_of=on_behalf_of,
        )

    def admin_dismiss(
        self,
        item_id: int,
        by_admin: str,
        *,
        reason: str,
    ) -> AttentionItem:
        return self._admin_transition(
            item_id, by_admin,
            new_status=_STATUS_DISMISSED,
            audit_action=_AUDIT_ACTION_DISMISS,
            reason=reason,
            on_behalf_of=None,
        )

    def _admin_transition(
        self,
        item_id: int,
        by_admin: str,
        *,
        new_status: str,
        audit_action: str,
        reason: str,
        on_behalf_of: str | None,
    ) -> AttentionItem:
        stripped_reason = _validate_admin_reason(item_id, reason)
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
            resolved_by=by_admin,
            resolution_note=item.resolution_note,
            updated_at=item.updated_at,
        )
        self._items[item_id] = updated
        self._audit.append({
            "actor_username": by_admin,
            "actor_role": "admin",
            "action": audit_action,
            "target_kind": "attention_item",
            "target_id": str(item_id),
            "before_value": _STATUS_OPEN,
            "after_value": new_status,
            "reason": stripped_reason,
            "resolved_on_behalf_of": on_behalf_of,
            "created_at": now,
        })
        return updated

    def upsert_by_case_and_type(
        self,
        *,
        case_id: str,
        item_type: str,
        affected_user: str,
        severity: str,
        details: str,
    ) -> AttentionItem:
        if item_type != "phi_redacted":
            raise ValueError(
                f"upsert_by_case_and_type only supports type='phi_redacted'; "
                f"got {item_type!r}"
            )
        now = utcnow()
        # Find an existing open row keyed by (case_id, type). Mirrors the
        # SQLite partial unique index — only open rows are coalesced;
        # resolved/dismissed history is preserved (no schema constraint
        # against multiple closed rows for the same case).
        existing_id: int | None = None
        for it in self._items.values():
            if (
                it.case_id == case_id
                and it.type == item_type
                and it.status == _STATUS_OPEN
            ):
                existing_id = it.id
                break
        if existing_id is not None:
            existing = self._items[existing_id]
            updated = AttentionItem(
                id=existing.id,
                type=existing.type,
                case_id=existing.case_id,
                affected_user=existing.affected_user,
                severity=severity,
                details=details,
                status=existing.status,
                created_at=existing.created_at,
                created_by=existing.created_by,
                resolved_at=existing.resolved_at,
                resolved_by=existing.resolved_by,
                resolution_note=existing.resolution_note,
                updated_at=now,
            )
            self._items[existing_id] = updated
            return updated
        # Insert path. Allocate a new id consistent with the rest of the
        # test fake — autoincrement off max existing id, falling back
        # to 1 when empty.
        new_id = (max(self._items, default=0) + 1)
        inserted = AttentionItem(
            id=new_id,
            type=item_type,
            case_id=case_id,
            affected_user=affected_user,
            severity=severity,
            details=details,
            status=_STATUS_OPEN,
            created_at=now,
            created_by="system_worker",
            resolved_at=None,
            resolved_by=None,
            resolution_note=None,
            updated_at=now,
        )
        self._items[new_id] = inserted
        if case_id is not None:
            self._flagged.add(case_id)
        return inserted

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
            updated_at=item.updated_at,
        )
        self._items[item_id] = updated
        self._audit.append({
            "actor_username": by,
            "actor_role": "surgeon",
            "action": audit_action,
            "target_kind": "attention_item",
            "target_id": str(item_id),
            "before_value": _STATUS_OPEN,
            "after_value": new_status,
            "reason": SURGEON_AUDIT_REASON,
            "resolved_on_behalf_of": None,
            "created_at": now,
        })
        return updated
