"""Tests for the extended ``AttentionItemsRepository`` surface — the
list / resolve / dismiss / count_actions_today methods Brief #3 added.

Tests are parametrized over both the SQLite and in-memory
implementations via the ``repo`` fixture so the two stay in lockstep.
A drift between them is the exact failure mode that motivates the
parametrization (the in-memory fake is the test-time substitute for
the SQLite repo; behavior must match)."""

from __future__ import annotations

import sqlite3

import pytest

from app.attention_actions import SURGEON_ACTION_BY_TYPE
from app.db.connection import connect, utcnow
from app.exceptions import ScopeViolationError
from app.repos.attention import (
    AttentionItem,
    AttentionItemActionMismatchError,
    AttentionItemAlreadyClosedError,
    AttentionItemNotFoundError,
    InMemoryAttentionItemsRepository,
    SqliteAttentionItemsRepository,
    SURGEON_AUDIT_REASON,
)


_SEED_TS = "2026-05-15T00:00:00+00:00"


def _make_item(
    *,
    item_id: int,
    item_type: str = "verify_soft_fail",
    case_id: str | None = "UCD-FIL-001",
    affected_user: str = "asarin",
    severity: str = "normal",
    details: str = "x",
    status: str = "open",
    created_at: str = _SEED_TS,
    # Default to a seeded user so the FK passes without the test having
    # to bootstrap the production system_worker row.
    created_by: str = "asarin",
    resolved_at: str | None = None,
    resolved_by: str | None = None,
    resolution_note: str | None = None,
) -> AttentionItem:
    return AttentionItem(
        id=item_id,
        type=item_type,
        case_id=case_id,
        affected_user=affected_user,
        severity=severity,
        details=details,
        status=status,
        created_at=created_at,
        created_by=created_by,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
        resolution_note=resolution_note,
    )


def _seed_sqlite_item(db_path, item: AttentionItem) -> int:
    """Insert one row, returning its actual id (SQLite assigns
    autoincrement; the caller's requested id is honored explicitly)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.execute(
            "INSERT INTO attention_items "
            "(id, type, case_id, affected_user, severity, details, "
            " created_at, created_by, updated_at, status, "
            " resolved_at, resolved_by, resolution_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id, item.type, item.case_id, item.affected_user,
                item.severity, item.details, item.created_at,
                item.created_by, item.updated_at or item.created_at,
                item.status, item.resolved_at,
                item.resolved_by, item.resolution_note,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture(params=["sqlite", "inmem"])
def repo(request, app_env):
    """Parametrized over both impls. Returns a (repo, seed_fn) pair so
    each test seeds via the impl-appropriate path."""
    kind = request.param
    if kind == "sqlite":
        r = SqliteAttentionItemsRepository()

        def seed(item: AttentionItem) -> int:
            return _seed_sqlite_item(app_env, item)

        return r, seed

    fake = InMemoryAttentionItemsRepository()

    def seed(item: AttentionItem) -> int:
        fake._items[item.id] = item
        return item.id

    return fake, seed


# ----- list_for_user -----


def test_list_for_user_returns_only_caller_items(repo):
    r, seed = repo
    seed(_make_item(item_id=1, affected_user="asarin"))
    seed(_make_item(item_id=2, affected_user="asarin"))
    seed(_make_item(item_id=3, affected_user="anoren"))
    out = r.list_for_user("asarin")
    ids = sorted([it.id for it in out])
    assert ids == [1, 2]


def test_list_for_user_sorted_newest_first(repo):
    r, seed = repo
    seed(_make_item(item_id=1, created_at="2026-05-10T08:00:00+00:00"))
    seed(_make_item(item_id=2, created_at="2026-05-15T08:00:00+00:00"))
    seed(_make_item(item_id=3, created_at="2026-05-12T08:00:00+00:00"))
    out = r.list_for_user("asarin")
    assert [it.id for it in out] == [2, 3, 1]


def test_list_for_user_tiebreaks_by_id_desc(repo):
    """Same created_at across multiple rows → higher id wins (most
    recent insert at the top of the list)."""
    r, seed = repo
    same_ts = "2026-05-15T08:00:00+00:00"
    seed(_make_item(item_id=1, created_at=same_ts))
    seed(_make_item(item_id=5, created_at=same_ts))
    seed(_make_item(item_id=3, created_at=same_ts))
    out = r.list_for_user("asarin")
    assert [it.id for it in out] == [5, 3, 1]


def test_list_for_user_status_filter_resolved(repo):
    r, seed = repo
    seed(_make_item(item_id=1, status="open"))
    seed(_make_item(item_id=2, status="resolved", resolved_by="asarin"))
    seed(_make_item(item_id=3, status="dismissed", resolved_by="asarin"))
    assert [it.id for it in r.list_for_user("asarin", "resolved")] == [2]


def test_list_for_user_status_filter_dismissed(repo):
    r, seed = repo
    seed(_make_item(item_id=1, status="open"))
    seed(_make_item(item_id=2, status="dismissed", resolved_by="asarin"))
    assert [it.id for it in r.list_for_user("asarin", "dismissed")] == [2]


def test_list_for_user_default_status_is_open(repo):
    r, seed = repo
    seed(_make_item(item_id=1, status="open"))
    seed(_make_item(item_id=2, status="resolved", resolved_by="asarin"))
    out = r.list_for_user("asarin")
    assert [it.id for it in out] == [1]


def test_list_for_user_empty_when_no_items(repo):
    r, _seed = repo
    assert r.list_for_user("anoren") == []


def test_list_for_user_returns_attention_item_dataclasses(repo):
    r, seed = repo
    seed(_make_item(
        item_id=1, item_type="pipeline_failure",
        severity="high", case_id="UCD-FIL-007", details="boom",
    ))
    out = r.list_for_user("asarin")
    assert len(out) == 1
    item = out[0]
    assert isinstance(item, AttentionItem)
    assert item.type == "pipeline_failure"
    assert item.severity == "high"
    assert item.case_id == "UCD-FIL-007"
    assert item.details == "boom"


# ----- resolve / dismiss happy paths -----


def test_resolve_flips_status_and_returns_updated_row(repo):
    r, seed = repo
    seed(_make_item(item_id=42, item_type="pipeline_failure"))
    out = r.resolve(42, by="asarin")
    assert out.id == 42
    assert out.status == "resolved"
    assert out.resolved_by == "asarin"
    assert out.resolved_at  # populated, ISO 8601
    # No longer surfaces in the open list.
    assert r.list_for_user("asarin") == []


def test_dismiss_flips_status_and_returns_updated_row(repo):
    r, seed = repo
    seed(_make_item(item_id=42, item_type="verify_soft_fail"))
    out = r.dismiss(42, by="asarin")
    assert out.status == "dismissed"
    assert out.resolved_by == "asarin"
    assert r.list_for_user("asarin") == []


# ----- validation gates -----


def test_resolve_unknown_id_raises_not_found(repo):
    r, _seed = repo
    with pytest.raises(AttentionItemNotFoundError) as exc_info:
        r.resolve(999, by="asarin")
    assert exc_info.value.item_id == 999


def test_dismiss_unknown_id_raises_not_found(repo):
    r, _seed = repo
    with pytest.raises(AttentionItemNotFoundError):
        r.dismiss(999, by="asarin")


def test_resolve_already_closed_raises_already_closed(repo):
    r, seed = repo
    seed(_make_item(
        item_id=42, item_type="pipeline_failure",
        status="resolved", resolved_by="asarin",
    ))
    with pytest.raises(AttentionItemAlreadyClosedError) as exc_info:
        r.resolve(42, by="asarin")
    assert exc_info.value.item_id == 42


def test_dismiss_already_closed_raises_already_closed(repo):
    r, seed = repo
    seed(_make_item(
        item_id=42, item_type="verify_soft_fail",
        status="dismissed", resolved_by="asarin",
    ))
    with pytest.raises(AttentionItemAlreadyClosedError):
        r.dismiss(42, by="asarin")


def test_resolve_on_dismiss_only_type_raises_action_mismatch(repo):
    """verify_soft_fail expects ``dismiss`` per SURGEON_ACTION_BY_TYPE.
    Catches a UI bypass where someone calls resolve on a dismiss-only
    type."""
    r, seed = repo
    seed(_make_item(item_id=42, item_type="verify_soft_fail"))
    with pytest.raises(AttentionItemActionMismatchError) as exc_info:
        r.resolve(42, by="asarin")
    assert exc_info.value.item_id == 42
    # Mutation guard: row stays open after the rejection.
    assert r.list_for_user("asarin")[0].status == "open"


def test_dismiss_on_resolve_only_type_raises_action_mismatch(repo):
    """pipeline_failure expects ``resolve``. Same guard, opposite
    direction."""
    r, seed = repo
    seed(_make_item(item_id=42, item_type="pipeline_failure"))
    with pytest.raises(AttentionItemActionMismatchError):
        r.dismiss(42, by="asarin")
    assert r.list_for_user("asarin")[0].status == "open"


def test_resolve_unknown_type_raises_action_mismatch(repo):
    """An item with a type the dispatch table doesn't cover can't be
    actioned from the surgeon UI at all (the card renders read-only).
    Direct repo calls also fail."""
    r, seed = repo
    seed(_make_item(item_id=42, item_type="some_future_type"))
    with pytest.raises(AttentionItemActionMismatchError):
        r.resolve(42, by="asarin")
    with pytest.raises(AttentionItemActionMismatchError):
        r.dismiss(42, by="asarin")


def test_resolve_by_wrong_user_raises_scope_violation(repo):
    """Cross-silo guard: a surgeon can only action their own items.
    Even if the dispatch verb is correct, the actor must own the row."""
    r, seed = repo
    seed(_make_item(
        item_id=42, item_type="pipeline_failure", affected_user="asarin",
    ))
    with pytest.raises(ScopeViolationError) as exc_info:
        r.resolve(42, by="anoren")
    assert exc_info.value.scope_at_time == "surgeon:anoren"
    assert "attention_item:42" in exc_info.value.resource


def test_dismiss_by_wrong_user_raises_scope_violation(repo):
    r, seed = repo
    seed(_make_item(
        item_id=42, item_type="verify_soft_fail", affected_user="asarin",
    ))
    with pytest.raises(ScopeViolationError):
        r.dismiss(42, by="anoren")


# ----- audit row contents -----


def _read_audit_rows(app_env) -> list[dict]:
    conn = connect(app_env)
    try:
        rows = conn.execute(
            "SELECT * FROM admin_audit ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_resolve_writes_audit_row_with_documented_shape(app_env):
    """Verified explicitly on the SQLite impl since the in-memory fake's
    audit format is for test introspection only — the production audit
    contract is the SQLite shape."""
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=42, item_type="pipeline_failure",
    ))
    r.resolve(42, by="asarin")
    rows = _read_audit_rows(app_env)
    assert len(rows) == 1
    audit = rows[0]
    assert audit["actor_username"] == "asarin"
    assert audit["actor_role"] == "surgeon"
    assert audit["action"] == "attention.resolve"
    assert audit["target_kind"] == "attention_item"
    assert audit["target_id"] == "42"
    assert audit["before_value"] == "open"
    assert audit["after_value"] == "resolved"
    assert audit["reason"] == SURGEON_AUDIT_REASON
    assert audit["resolved_on_behalf_of"] is None
    assert audit["created_at"]  # populated


def test_dismiss_writes_audit_row_with_documented_shape(app_env):
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=42, item_type="verify_soft_fail",
    ))
    r.dismiss(42, by="asarin")
    rows = _read_audit_rows(app_env)
    assert len(rows) == 1
    assert rows[0]["action"] == "attention.dismiss"
    assert rows[0]["after_value"] == "dismissed"


def test_inmem_audit_introspection(app_env):
    """The in-memory fake exposes ``audit`` as a property for tests —
    same shape as the SQLite rows so assertions can be parametrized."""
    fake = InMemoryAttentionItemsRepository(items=[
        _make_item(item_id=42, item_type="pipeline_failure"),
    ])
    fake.resolve(42, by="asarin")
    assert len(fake.audit) == 1
    assert fake.audit[0]["action"] == "attention.resolve"
    assert fake.audit[0]["target_id"] == "42"
    assert fake.audit[0]["reason"] == SURGEON_AUDIT_REASON


def test_rejected_action_writes_no_audit_row(app_env):
    """Validation gates run BEFORE the audit write — a rejected action
    must not leave an orphaned row implying the action happened."""
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=42, item_type="pipeline_failure",
    ))
    with pytest.raises(AttentionItemActionMismatchError):
        r.dismiss(42, by="asarin")  # type expects resolve
    assert _read_audit_rows(app_env) == []


# ----- count_actions_today -----


def test_count_actions_today_counts_resolve_and_dismiss(app_env):
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=1, item_type="pipeline_failure",
    ))
    _seed_sqlite_item(app_env, _make_item(
        item_id=2, item_type="verify_soft_fail",
    ))
    r.resolve(1, by="asarin")
    r.dismiss(2, by="asarin")
    cnt = r.count_actions_today("asarin", "2000-01-01T00:00:00+00:00")
    assert cnt == 2


def test_count_actions_today_filters_by_username(app_env):
    """Cross-actor isolation: only the named user's audit rows count."""
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=1, item_type="pipeline_failure",
    ))
    _seed_sqlite_item(app_env, _make_item(
        item_id=2, item_type="verify_soft_fail", affected_user="anoren",
    ))
    r.resolve(1, by="asarin")
    r.dismiss(2, by="anoren")
    assert r.count_actions_today(
        "asarin", "2000-01-01T00:00:00+00:00"
    ) == 1
    assert r.count_actions_today(
        "anoren", "2000-01-01T00:00:00+00:00"
    ) == 1


def test_count_actions_today_respects_cutoff(app_env):
    """Cutoff filter — rows before the start_iso don't count. Verifies
    the WHERE created_at >= ? clause is wired correctly."""
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=1, item_type="pipeline_failure",
    ))
    r.resolve(1, by="asarin")
    # Future cutoff → zero matches.
    future = "2099-01-01T00:00:00+00:00"
    assert r.count_actions_today("asarin", future) == 0


def test_count_actions_today_inmem_parity():
    fake = InMemoryAttentionItemsRepository(items=[
        _make_item(item_id=1, item_type="pipeline_failure"),
    ])
    fake.resolve(1, by="asarin")
    assert fake.count_actions_today(
        "asarin", "2000-01-01T00:00:00+00:00"
    ) == 1


# ----- has_attention_for_case_ids parity (Brief #2 surface preserved) -----


def test_has_attention_still_works_after_repo_extension(app_env):
    """The Brief #2 surface must keep working after Brief #3 extends
    the protocol. Catches a regression where the new methods break the
    old ones."""
    r = SqliteAttentionItemsRepository()
    _seed_sqlite_item(app_env, _make_item(
        item_id=1, case_id="UCD-FIL-001",
    ))
    out = r.has_attention_for_case_ids(["UCD-FIL-001", "UCD-FIL-002"])
    assert out == {"UCD-FIL-001": True, "UCD-FIL-002": False}
