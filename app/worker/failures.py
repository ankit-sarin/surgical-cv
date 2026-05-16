"""Attention-item writes + marker archival.

After ``dispatch_marker`` returns, the worker calls into here to record the
outcome (attention_items row if non-success) and move the marker to the
appropriate quarantine subdir.

The system_worker user is the ``created_by`` on every row this module writes;
it's idempotently upserted into the users table at worker startup so the
FK never fires."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.db.connection import connect, utcnow

from app.worker.dispatch import DispatchOutcome
from app.worker.scan import Marker, MalformedMarker


_log = logging.getLogger(__name__)

# F-030: surgeon-facing message for malformed-marker quarantines. Generic by
# design — no path, no parse-error text. Operators get the full context
# (marker path + parse-failure reason) via the journalctl-captured logger
# above; the surgeon sees an actionable instruction in the Action Required
# tab instead of internal NAS path strings.
_MALFORMED_GENERIC_MSG = (
    "A submitted case could not be processed. Please contact your coordinator."
)

SYSTEM_WORKER_USERNAME = "system_worker"

# Quarantine subdir names, relative to the marker's raw-<surgeon> parent.
_PROCESSED = ".processed"
_FAILED = ".failed"
_MALFORMED = ".malformed"

# attention_items.type discriminators.
TYPE_PIPELINE_FAILURE = "pipeline_failure"
TYPE_VERIFY_SOFT_FAIL = "verify_soft_fail"
TYPE_ORPHAN_MARKER = "orphan_marker"
TYPE_MALFORMED_MARKER = "malformed_marker"


def ensure_system_worker_user() -> None:
    """Idempotent upsert of the system_worker admin row. Allows attention_items
    writes to satisfy the ``created_by`` FK without a manual seed step.

    F-010: explicit ``try/finally: conn.close()`` (matches the pattern in
    ``app/auth.py:lookup_active_user`` and ``app/main.py:_log_violation``).
    ``with connect() as conn`` would commit/rollback the transaction but NOT
    close the connection — sqlite3.Connection.__exit__ only handles the
    transaction lifecycle, not the FD lifecycle. Under ``--daemon`` mode the
    leak compounds per iteration; under ``--once`` the kernel reclaims FDs at
    process exit but we close explicitly anyway for symmetry across modes."""
    conn = connect()
    try:
        existing = conn.execute(
            "SELECT username FROM users WHERE username = ?",
            (SYSTEM_WORKER_USERNAME,),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, "
            "active, created_at) VALUES (?, 'admin', NULL, NULL, 0, ?)",
            (SYSTEM_WORKER_USERNAME, utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def _lookup_username_for_slug(folder_slug: str) -> str:
    """Map a surgeon folder_slug → users.username (active surgeon row).
    Returns ``SYSTEM_WORKER_USERNAME`` if no match (so attention_items still
    have a valid FK; details carry the slug for triage)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username FROM users WHERE folder_slug = ? "
            "AND role = 'surgeon' AND active = 1 LIMIT 1",
            (folder_slug,),
        ).fetchone()
        if row is None:
            return SYSTEM_WORKER_USERNAME
        return row["username"]
    finally:
        conn.close()


def _ensure_subdir(parent: Path, name: str) -> Path:
    sub = parent / name
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def archive_marker(marker_path: Path, kind: str) -> Path:
    """Move the marker to its terminal subdir. ``kind`` ∈ {success, fail,
    malformed}. Overwrites any pre-existing destination (re-trigger case)."""
    parent = marker_path.parent
    if kind == "success":
        dest = _ensure_subdir(parent, _PROCESSED) / marker_path.name
    elif kind == "fail":
        dest = _ensure_subdir(parent, _FAILED) / marker_path.name
    elif kind == "malformed":
        dest = _ensure_subdir(parent, _MALFORMED) / marker_path.name
    else:
        raise ValueError(f"unknown archive kind: {kind!r}")
    # shutil.move handles cross-filesystem if the lock file path differs;
    # for same-fs (the common case) it's an os.rename internally.
    shutil.move(str(marker_path), str(dest))
    return dest


def write_attention_item(
    *,
    item_type: str,
    affected_user: str,
    case_id: str | None,
    severity: str,
    details: str,
) -> int:
    """Insert one attention_items row. Returns the new row id.

    Brief #3.5b: ``updated_at`` is set equal to ``created_at`` at
    insert time so first-emit rows have a sensible value without
    needing a separate UPDATE. The upsert path
    (``upsert_by_case_and_type`` on the repo) advances ``updated_at``
    on conflict; this plain INSERT path doesn't see conflicts because
    its callers (verify_soft_fail / pipeline_failure / orphan /
    malformed) aren't constrained by the phi-redacted-only unique
    index."""
    now = utcnow()
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO attention_items "
            "(type, case_id, affected_user, severity, details, "
            " created_at, created_by, updated_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (
                item_type,
                case_id,
                affected_user,
                severity,
                details,
                now,
                SYSTEM_WORKER_USERNAME,
                now,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def record_dispatch_outcome(marker: Marker, outcome: DispatchOutcome) -> None:
    """Translate a DispatchOutcome into the right attention_items + archival
    behavior. Pure side effects — no return value."""
    affected_user = _lookup_username_for_slug(marker.surgeon)

    if outcome.kind == "success":
        archive_marker(marker.path, "success")
        return

    if outcome.kind == "soft_fail":
        write_attention_item(
            item_type=TYPE_VERIFY_SOFT_FAIL,
            affected_user=affected_user,
            case_id=marker.ucd_fil_id,
            severity="normal",
            details=(
                f"verify diagnostician returned a clean-fail verdict; "
                f"case is terminal-but-flagged. detail: {outcome.detail}"
            ),
        )
        # Soft fail is terminal — case is done, not retryable. .processed/.
        archive_marker(marker.path, "success")
        return

    if outcome.kind == "orphan":
        write_attention_item(
            item_type=TYPE_ORPHAN_MARKER,
            affected_user=affected_user,
            case_id=marker.ucd_fil_id,
            severity="high",
            details=(
                f"marker references case_id {marker.ucd_fil_id!r} that has "
                f"no row in case_manifest.csv. detail: {outcome.detail}"
            ),
        )
        archive_marker(marker.path, "fail")
        return

    # hard_fail
    write_attention_item(
        item_type=TYPE_PIPELINE_FAILURE,
        affected_user=affected_user,
        case_id=marker.ucd_fil_id,
        severity="high",
        details=(
            f"pipeline {outcome.stage} stage failed "
            f"(returncode={outcome.returncode}): {outcome.detail}"
        ),
    )
    archive_marker(marker.path, "fail")


def record_malformed(marker: MalformedMarker) -> None:
    """Log + quarantine path for parse-time failures. No surgeon lookup
    available (the marker is unparsed); affected_user falls back to
    system_worker — these rows show up only in the admin AR (Brief #4
    cross-silo list), never in any surgeon's AR tab.

    Brief #4: ``details`` now carries the marker filename basename so
    the admin can identify which file failed. Filenames are not PHI
    (the marker filename embeds the study code ``UCD-FIL-NNN`` only;
    even malformed ones contain at most a corrupted/truncated case id).
    Full path + parse-error text remain in the systemd journal — those
    can leak NAS paths and Python tracebacks, which still don't belong
    on the AR card.

    F-030 (pre-Brief-#4) chose the generic message for surgeon-visibility
    reasons; that concern doesn't apply here because malformed rows have
    ``affected_user = system_worker`` and never reach a surgeon UI."""
    _log.warning(
        "malformed marker",
        extra={
            "marker_path": str(marker.path),
            "parse_error": marker.reason,
        },
    )
    write_attention_item(
        item_type=TYPE_MALFORMED_MARKER,
        affected_user=SYSTEM_WORKER_USERNAME,
        case_id=None,
        severity="normal",
        details=f"{_MALFORMED_GENERIC_MSG} (file: {marker.path.name})",
    )
    archive_marker(marker.path, "malformed")
