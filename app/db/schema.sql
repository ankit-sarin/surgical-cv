-- Surgical CV application database schema (v18 baseline).
--
-- Applied by app/db/init_db.py via executescript(). PRAGMA foreign_keys = ON
-- is set per-connection in app/db/connection.py — not declared here, since
-- SQLite does not persist PRAGMA settings in the schema.
--
-- Timestamps: TEXT columns, ISO 8601 UTC strings set by Python at insert.
-- No DEFAULT CURRENT_TIMESTAMP — keeps test injection deterministic.

CREATE TABLE specialties (
    specialty_code TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at     TEXT NOT NULL
);

CREATE TABLE users (
    username       TEXT PRIMARY KEY,
    role           TEXT NOT NULL CHECK (role IN ('surgeon', 'admin')),
    folder_slug    TEXT,
    specialty      TEXT REFERENCES specialties(specialty_code),
    display_name   TEXT,
    email          TEXT,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at     TEXT NOT NULL,
    last_login_at  TEXT,
    notes          TEXT,
    CHECK (
        (role = 'surgeon' AND folder_slug IS NOT NULL AND specialty IS NOT NULL)
     OR (role = 'admin'   AND folder_slug IS NULL     AND specialty IS NULL)
    )
);

CREATE INDEX idx_users_role      ON users (role);
CREATE INDEX idx_users_specialty ON users (specialty);

CREATE TABLE picklist_values (
    field         TEXT NOT NULL,
    value         TEXT NOT NULL,
    display_label TEXT NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    specialty     TEXT REFERENCES specialties(specialty_code),
    created_at    TEXT NOT NULL,
    created_by    TEXT REFERENCES users(username),
    PRIMARY KEY (field, value)
);

-- Dropdown query: WHERE field = ? AND specialty = ? AND active = 1 ORDER BY sort_order.
CREATE INDEX idx_picklist_dropdown
    ON picklist_values (field, specialty, active, sort_order);

CREATE TABLE attention_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,
    case_id         TEXT,
    affected_user   TEXT NOT NULL REFERENCES users(username),
    severity        TEXT NOT NULL DEFAULT 'normal',
    details         TEXT,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL REFERENCES users(username),
    -- Brief #3.5b: ``updated_at`` advances on every upsert via
    -- ``upsert_by_case_and_type``. For first-insert paths (the
    -- existing ``write_attention_item`` flow used by hard/soft fail /
    -- orphan / malformed emits) the row writer sets ``updated_at =
    -- created_at`` at insert time. NOT NULL is intentional — no
    -- ambiguous "we don't know when it was last touched" state.
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolved_at     TEXT,
    resolved_by     TEXT REFERENCES users(username),
    resolution_note TEXT
);

-- Surgeon's "Action Required" pull: WHERE affected_user = ? AND status = 'open'.
CREATE INDEX idx_attention_affected_status
    ON attention_items (affected_user, status);
-- Admin's heterogeneous queue: WHERE status = 'open' ORDER BY created_at DESC.
CREATE INDEX idx_attention_status_created
    ON attention_items (status, created_at);
-- Brief #3.5b: enforce "exactly one phi_redacted row per case_id".
--
-- Scope narrowed from the brief's original spec
-- (``WHERE case_id IS NOT NULL``) to phi_redacted only. Reason:
-- broadening the uniqueness to all per-case rollup types
-- (verify_soft_fail, pipeline_failure, orphan_marker) would break
-- existing retry semantics where operators re-trigger a failed case
-- by moving its marker back from ``.failed/`` — the second dispatch
-- emits another rollup row of the same type, today a plain INSERT.
-- Converting all those call sites to upsert is broader than this
-- brief's stated cardinality intent. Per the brief's contract table:
-- "Cardinality: Exactly one row per (case_id, type='phi_redacted')."
-- The narrower index satisfies that exactly without forcing the
-- companion refactors. If we later decide the other rollup types
-- should also coalesce on retry, expand the WHERE clause or drop
-- it entirely.
CREATE UNIQUE INDEX idx_attention_phi_redacted_case_uniq
    ON attention_items (case_id)
    WHERE case_id IS NOT NULL AND type = 'phi_redacted';

-- Brief #4: ``admin_audit`` is the audit log for state-changing actions
-- against attention_items (and future admin-mediated mutations). The
-- column name ``admin_username`` was misleading — surgeon self-service
-- resolve/dismiss writes here too. The rename + new ``actor_role``
-- discriminator (one of 'surgeon' / 'admin') keeps the table honest
-- about who actually performed the action.
--
-- ``resolved_on_behalf_of`` is non-null only for admin-mediated
-- "resolve on behalf of surgeon" actions; it carries the surgeon's
-- username so the relationship is queryable rather than buried in
-- the free-text ``reason``.
--
-- No DEFAULT on ``actor_role`` — application code MUST specify it on
-- every insert. (The migration step uses a DEFAULT of 'admin' for the
-- backfill of pre-existing rows, then drops it after data is in place
-- so this schema.sql reflects the steady-state shape.)
CREATE TABLE admin_audit (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_username        TEXT NOT NULL REFERENCES users(username),
    actor_role            TEXT NOT NULL CHECK (actor_role IN ('surgeon', 'admin')),
    action                TEXT NOT NULL,
    target_kind           TEXT NOT NULL,
    target_id             TEXT NOT NULL,
    before_value          TEXT,
    after_value           TEXT,
    reason                TEXT NOT NULL,
    resolved_on_behalf_of TEXT REFERENCES users(username),
    created_at            TEXT NOT NULL
);

-- Time-range audit queries (audit log review by date window).
CREATE INDEX idx_admin_audit_created ON admin_audit (created_at);

CREATE TABLE scope_violation_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT NOT NULL REFERENCES users(username),
    attempted_resource TEXT NOT NULL,
    attempted_action   TEXT NOT NULL,
    scope_at_time      TEXT,
    user_agent         TEXT,
    created_at         TEXT NOT NULL
);

-- Per-user violation history (newest first).
CREATE INDEX idx_violation_user_created
    ON scope_violation_log (username, created_at);
