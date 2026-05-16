"""Admin Gradio Blocks app — Global Dashboard + Action Required tabs.

Brief #4 fills in the admin surface. Two tabs:

  1. **Global Dashboard** — five-stat strip + per-surgeon DataFrame.
     Single ``gr.HTML`` for the stats block (avoids the Brief #3.1
     reactive-flush cycle by construction); ``gr.DataFrame`` for the
     per-surgeon table, refreshed on tab activation.

  2. **Action Required** — cross-silo list of every open attention item
     with type / surgeon / severity / age filters. Row select opens a
     detail panel with dismiss / resolve-on-behalf admin actions; both
     require a reason of ``ADMIN_REASON_MIN_LENGTH`` chars or more.

The repo-layer admin methods (``admin_resolve`` / ``admin_dismiss``)
bypass the surgeon-side action-type validation and scope check — admin
is the override path. Audit rows record ``actor_role='admin'`` and the
new ``resolved_on_behalf_of`` column when resolving on a surgeon's
behalf.

Render path notes
-----------------

Tab activation refresh: Gradio 6 surfaces ``gr.Tab.select`` as the
event for tab activation. Each tab wires its render function to both
``blocks.load`` (initial mount) and ``gr.Tab.select`` (re-fetch on
re-activation), so a stale state never lingers after the admin acts
on something in another tab.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import gradio as gr

from app.attention_actions import display_for_type
from app.auth import (
    SESSION_COOKIE_NAME,
    decode_session,
    identity_string_for_request,
    lookup_active_user,
)
from app.db.connection import connect
from app.repos import (
    AttentionItem,
    AttentionItemAlreadyClosedError,
    AttentionItemNotFoundError,
    CsvCaseRepository,
    CsvCaseManifestRepository,
    CsvPipelineStateRepository,
    FilesystemRawSegmentRepository,
    Repos,
    SqliteAttentionItemsRepository,
    SqlitePicklistRepository,
)
from app.repos.attention import ADMIN_REASON_MIN_LENGTH
from app.scopes import AdminScope


# ----- per-request scope construction -----


def _scope_from_request(request: gr.Request | None) -> AdminScope | None:
    """Build an :class:`AdminScope` for the authenticated admin behind
    ``request``. Returns ``None`` (rather than raising) for any
    auth-related miss so callers can degrade to an empty view rather
    than crash the page render. The auth-dep at the FastAPI mount has
    already gated the request — this lookup is informational."""
    if request is None:
        return None
    cookies = getattr(request, "cookies", None) or {}
    username = decode_session(cookies.get(SESSION_COOKIE_NAME))
    if not username:
        return None
    user = lookup_active_user(username)
    if user is None or user["role"] != "admin":
        return None
    repos = Repos(
        case=CsvCaseRepository(),
        segment=FilesystemRawSegmentRepository(),
        picklist=SqlitePicklistRepository(),
        pipeline_state=CsvPipelineStateRepository(),
        attention=SqliteAttentionItemsRepository(),
        case_manifest=CsvCaseManifestRepository(),
    )
    return AdminScope(user["username"], repos)


# ----- shared helpers -----


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _state_latest_ts(state: dict | None) -> datetime | None:
    if state is None:
        return None
    for key in ("verify_ts", "deid_ts", "concat_ts", "intake_ts"):
        ts = _parse_iso_or_none(state.get(key))
        if ts is not None:
            return ts
    return None


def _identity(request: gr.Request) -> str:
    # Append a logout link so the user has an explicit sign-out
    # affordance. ``/logout`` is the role-agnostic FastAPI route that
    # clears the session cookie and redirects to ``/login``.
    return f"{identity_string_for_request(request)} · [Sign out](/logout)"


def _list_surgeon_users() -> list[dict]:
    """Read the seeded surgeon roster from app.db. Returns one dict per
    active surgeon with ``username``, ``folder_slug``, and
    ``display_name``. Inactive users skipped — the dashboard summarizes
    who can submit today, not historical accounts. Empty list on any
    DB error so the dashboard renders rather than 500s."""
    try:
        conn = connect()
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            "SELECT username, folder_slug, display_name "
            "FROM users WHERE role = 'surgeon' AND active = 1 "
            "ORDER BY username"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ===== Global Dashboard tab =====


# Brief #3.1 lesson learned: custom CSS rules on layout-affecting
# properties (flex-wrap, margin: auto, transitions) can trigger
# Svelte 5's effect_update_depth_exceeded cycle when Gradio's
# reactive flush meets the browser's resize observer. We use a CSS
# grid with fixed columns (no wrap decision) + inline styles instead
# of a separate stylesheet to sidestep that whole class of issues.
_STAT_STRIP_TEMPLATE = """
<div style="display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin: 12px 0 18px 0;">
  <div style="padding: 14px 18px; background: #ffffff; border: 1px solid rgba(10, 94, 86, 0.18); border-radius: 10px; text-align: center;">
    <div style="font-size: 28px; font-weight: 600; color: #0A5E56; line-height: 1.15;">{total_cases}</div>
    <div style="font-size: 12px; color: #2C2C2C; margin-top: 4px; letter-spacing: 0.02em; text-transform: uppercase;">Total cases</div>
  </div>
  <div style="padding: 14px 18px; background: #ffffff; border: 1px solid rgba(10, 94, 86, 0.18); border-radius: 10px; text-align: center;">
    <div style="font-size: 28px; font-weight: 600; color: #0A5E56; line-height: 1.15;">{intake_cases}</div>
    <div style="font-size: 12px; color: #2C2C2C; margin-top: 4px; letter-spacing: 0.02em; text-transform: uppercase;">In intake</div>
  </div>
  <div style="padding: 14px 18px; background: #ffffff; border: 1px solid rgba(10, 94, 86, 0.18); border-radius: 10px; text-align: center;">
    <div style="font-size: 28px; font-weight: 600; color: #0A5E56; line-height: 1.15;">{open_ar}</div>
    <div style="font-size: 12px; color: #2C2C2C; margin-top: 4px; letter-spacing: 0.02em; text-transform: uppercase;">Open AR items</div>
  </div>
  <div style="padding: 14px 18px; background: #ffffff; border: 1px solid rgba(10, 94, 86, 0.18); border-radius: 10px; text-align: center;">
    <div style="font-size: 28px; font-weight: 600; color: #0A5E56; line-height: 1.15;">{high_ar}</div>
    <div style="font-size: 12px; color: #2C2C2C; margin-top: 4px; letter-spacing: 0.02em; text-transform: uppercase;">High-severity AR</div>
  </div>
  <div style="padding: 14px 18px; background: #ffffff; border: 1px solid rgba(10, 94, 86, 0.18); border-radius: 10px; text-align: center;">
    <div style="font-size: 28px; font-weight: 600; color: #0A5E56; line-height: 1.15;">{stale_cases}</div>
    <div style="font-size: 12px; color: #2C2C2C; margin-top: 4px; letter-spacing: 0.02em; text-transform: uppercase;">Stale (&gt; 7d)</div>
  </div>
</div>
"""


_PER_SURGEON_HEADERS = (
    "Surgeon", "Total cases", "In progress", "Verified",
    "Open AR items", "High-severity AR",
)
_PER_SURGEON_DATATYPES = ["str"] * len(_PER_SURGEON_HEADERS)


def _compute_dashboard(scope: AdminScope) -> tuple[str, list[list]]:
    """Compute the dashboard payload — the five stat values and the
    per-surgeon table. One pass over the relevant repos; the surgeon
    grouping happens in Python because we want consistent ordering
    (alphabetical by username) regardless of repo storage order."""
    all_cases = scope.repos.case.list_all()
    all_states = {
        s["ucd_fil_id"]: s
        for s in scope.repos.pipeline_state.list_all()
    }
    open_items = scope.repos.attention.list_all(status="open")

    surgeon_users = _list_surgeon_users()

    # Per-surgeon counters: keyed by folder_slug for case grouping +
    # by username for AR grouping (AR.affected_user is username, not
    # folder_slug). We need both keys to join — build a folder-slug →
    # username map.
    slug_to_username = {
        u["folder_slug"]: u["username"] for u in surgeon_users
    }

    by_slug_total: dict[str, int] = defaultdict(int)
    by_slug_in_progress: dict[str, int] = defaultdict(int)
    by_slug_verified: dict[str, int] = defaultdict(int)
    intake_total = 0
    stale_total = 0
    now = _utcnow()
    seven_days_ago = now.timestamp() - 7 * 24 * 3600

    for case in all_cases:
        slug = case.get("surgeon", "") or ""
        by_slug_total[slug] += 1
        state = all_states.get(case.get("ucd_fil_id", ""))
        stage_str = ""
        if state is not None:
            stage = state.get("stage")
            stage_str = stage.value if hasattr(stage, "value") else str(stage)
        if stage_str == "intake":
            intake_total += 1
        if stage_str == "verified":
            by_slug_verified[slug] += 1
        elif stage_str and stage_str != "intake":
            # concatenated / deidentified — actively being processed
            by_slug_in_progress[slug] += 1
        latest = _state_latest_ts(state)
        if latest is None or latest.timestamp() < seven_days_ago:
            stale_total += 1

    by_username_open: dict[str, int] = defaultdict(int)
    by_username_high: dict[str, int] = defaultdict(int)
    for item in open_items:
        by_username_open[item.affected_user] += 1
        if (item.severity or "").lower() == "high":
            by_username_high[item.affected_user] += 1

    stats = {
        "total_cases": len(all_cases),
        "intake_cases": intake_total,
        "open_ar": len(open_items),
        "high_ar": sum(by_username_high.values()),
        "stale_cases": stale_total,
    }
    strip_html = _STAT_STRIP_TEMPLATE.format(**stats)

    table_rows: list[list] = []
    for user in surgeon_users:
        slug = user["folder_slug"]
        username = user["username"]
        table_rows.append([
            user.get("display_name") or username,
            by_slug_total.get(slug, 0),
            by_slug_in_progress.get(slug, 0),
            by_slug_verified.get(slug, 0),
            by_username_open.get(username, 0),
            by_username_high.get(username, 0),
        ])

    return strip_html, table_rows


def render_dashboard(request: gr.Request) -> tuple:
    """Wired to ``blocks.load`` + the Global Dashboard tab's ``select``
    event. Two outputs: stat-strip HTML and per-surgeon table rows."""
    scope = _scope_from_request(request)
    if scope is None:
        # Defensive: auth-dep should have prevented this, but render
        # an empty shell rather than crash.
        return _STAT_STRIP_TEMPLATE.format(
            total_cases=0, intake_cases=0, open_ar=0, high_ar=0,
            stale_cases=0,
        ), gr.update(value=[])
    strip_html, table_rows = _compute_dashboard(scope)
    return strip_html, gr.update(value=table_rows)


# ===== Action Required tab =====


_AR_HEADERS = (
    "Surgeon", "Case", "Type", "Severity", "Age (d)", "Details",
)
_AR_DATATYPES = ["str"] * len(_AR_HEADERS)


_ALL_SEVERITIES_LABEL = "All"
# Severity vocabulary in use today (worker emits these). The brief lists
# info/low/medium/high/critical as suggestions; we mirror the actual
# emitted values so the filter dropdown can't offer options that match
# zero rows. Add severities here as the worker emits them.
_SEVERITY_FILTER_OPTIONS = [_ALL_SEVERITIES_LABEL, "normal", "high"]

_ALL_SURGEONS_LABEL = "All surgeons"
_ALL_TYPES_LABEL = "All types"


@dataclass(frozen=True)
class _ARRow:
    """One AR row for the cross-silo list. Keyed by item_id so the
    detail-panel handler can look back to the canonical
    ``AttentionItem`` after a row select event."""
    item_id: int
    surgeon_label: str
    case_id: str
    item_type: str
    type_label: str
    severity: str
    age_days: int
    details_short: str
    details_full: str
    affected_user: str


def _truncate(text: str | None, limit: int = 80) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _age_days(item: AttentionItem, now: datetime) -> int:
    """Whole days since the item was created. Rounds down — a 23h-old
    item shows ``0`` so the filter's "older than N days" reads
    inclusively (slider at 1 = include items aged 1+ days)."""
    created = _parse_iso_or_none(item.created_at)
    if created is None:
        return 0
    return max(0, int((now - created).total_seconds() // 86400))


def _affected_user_to_surgeon_label(
    affected_user: str, slug_for_user: dict[str, str]
) -> str:
    """Render the AR row's surgeon column. We surface the folder_slug
    when known (more recognizable than usernames for ops triage). Falls
    back to the username verbatim so system_worker rows are visible too."""
    slug = slug_for_user.get(affected_user)
    if slug:
        return slug
    return affected_user


def _ar_rows_from_items(
    items: Iterable[AttentionItem], slug_for_user: dict[str, str],
    now: datetime,
) -> list[_ARRow]:
    rows: list[_ARRow] = []
    for item in items:
        td = display_for_type(item.type)
        rows.append(_ARRow(
            item_id=item.id,
            surgeon_label=_affected_user_to_surgeon_label(
                item.affected_user, slug_for_user
            ),
            case_id=item.case_id or "",
            item_type=item.type,
            type_label=td.label,
            severity=item.severity or "",
            age_days=_age_days(item, now),
            details_short=_truncate(item.details, 80),
            details_full=item.details or "",
            affected_user=item.affected_user,
        ))
    return rows


def _ar_table_payload(rows: list[_ARRow]) -> list[list]:
    return [
        [r.surgeon_label, r.case_id, r.type_label, r.severity,
         str(r.age_days), r.details_short]
        for r in rows
    ]


def _apply_filters(
    rows: list[_ARRow],
    *,
    type_filter: str,
    surgeon_filter: str,
    severity_filter: str,
    age_filter: int,
) -> list[_ARRow]:
    """AND-together filter applier. ``"All ..."`` sentinel values short
    out a given dimension; the age slider at 0 means "no minimum age."""
    out: list[_ARRow] = []
    for r in rows:
        if type_filter != _ALL_TYPES_LABEL and r.item_type != type_filter:
            continue
        if (
            surgeon_filter != _ALL_SURGEONS_LABEL
            and r.surgeon_label != surgeon_filter
        ):
            continue
        if (
            severity_filter != _ALL_SEVERITIES_LABEL
            and r.severity != severity_filter
        ):
            continue
        if r.age_days < int(age_filter or 0):
            continue
        out.append(r)
    return out


def _detail_html(row: _ARRow) -> str:
    """Render the detail panel above the action buttons. PHI-safe:
    every value we surface (case_id, surgeon_label, type_label, severity,
    details_full) comes from already-sanitized fields — case_id is a
    study code, surgeon_label is a folder_slug, details was emitted
    through the worker's category-only formatter (Brief #3.5b)."""
    age = "1 day" if row.age_days == 1 else f"{row.age_days} days"
    return (
        '<div style="background: #ffffff; '
        'border: 1px solid rgba(10, 94, 86, 0.18); '
        'border-radius: 10px; padding: 14px 18px; margin-top: 12px;">'
        f"<p><strong>{row.type_label}</strong> for case "
        f"<code>{row.case_id or '—'}</code> "
        f"(surgeon: <code>{row.surgeon_label}</code>, "
        f"age: {age}, severity: {row.severity or '—'}).</p>"
        f'<p style="margin-top: 8px; color: #2C2C2C;">{row.details_full}</p>'
        "</div>"
    )


def _empty_detail_html() -> str:
    return (
        '<p style="color: #6c6c6c; font-style: italic; padding: 12px 0;">'
        'Select a row above to see details and take action.</p>'
    )


def render_ar(
    request: gr.Request,
    type_filter: str,
    surgeon_filter: str,
    severity_filter: str,
    age_filter: float,
) -> tuple:
    """Re-query the cross-silo AR list, apply filters, and update both
    the DataFrame + the row cache (gr.State stores the full _ARRow list
    so row-select can look back without re-querying)."""
    scope = _scope_from_request(request)
    if scope is None:
        return gr.update(value=[]), [], _empty_detail_html()

    items = scope.repos.attention.list_all(status="open")
    surgeon_users = _list_surgeon_users()
    slug_for_user = {u["username"]: u["folder_slug"] for u in surgeon_users}
    now = _utcnow()
    all_rows = _ar_rows_from_items(items, slug_for_user, now)
    filtered = _apply_filters(
        all_rows,
        type_filter=type_filter,
        surgeon_filter=surgeon_filter,
        severity_filter=severity_filter,
        age_filter=int(age_filter or 0),
    )
    return (
        gr.update(value=_ar_table_payload(filtered)),
        [
            {
                "item_id": r.item_id,
                "surgeon_label": r.surgeon_label,
                "case_id": r.case_id,
                "item_type": r.item_type,
                "type_label": r.type_label,
                "severity": r.severity,
                "age_days": r.age_days,
                "details_short": r.details_short,
                "details_full": r.details_full,
                "affected_user": r.affected_user,
            }
            for r in filtered
        ],
        _empty_detail_html(),
    )


def _on_row_select(
    cached_rows: list[dict],
    evt: gr.SelectData,
) -> tuple:
    """Row-select handler. Pulls the selected row out of ``cached_rows``
    (gr.State, set by ``render_ar``) and renders the detail panel +
    enables the action buttons."""
    if evt is None or evt.index is None:
        return _empty_detail_html(), gr.update(), "", ""
    # evt.index is [row, col] for DataFrame; we want just the row index.
    row_idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    if not isinstance(row_idx, int) or row_idx < 0 or row_idx >= len(cached_rows):
        return _empty_detail_html(), gr.update(), "", ""
    row = _ARRow(**cached_rows[row_idx])
    return (
        _detail_html(row),
        gr.update(value=row.item_id),  # hidden state stash for actions
        "",  # clear reason textbox
        "",  # clear error markdown
    )


def _admin_dismiss_handler(
    request: gr.Request,
    item_id: int | None,
    reason: str,
    type_filter: str,
    surgeon_filter: str,
    severity_filter: str,
    age_filter: float,
):
    """Dismiss action handler. Server-side reason validation runs first;
    invalid input returns an inline error markdown without firing the
    repo. Successful dismiss re-renders the table + clears the panel."""
    scope = _scope_from_request(request)
    if scope is None or item_id is None:
        return (
            gr.update(),  # df
            [],  # cached_rows
            _empty_detail_html(),
            gr.update(value=None),
            "",  # reason
            "**Session expired — please sign in again.**",  # error md
        )
    stripped = (reason or "").strip()
    if len(stripped) < ADMIN_REASON_MIN_LENGTH:
        return (
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            reason,
            f"**A reason of at least {ADMIN_REASON_MIN_LENGTH} "
            f"characters is required.**",
        )
    try:
        scope.repos.attention.admin_dismiss(
            int(item_id), scope.username, reason=stripped,
        )
    except AttentionItemNotFoundError:
        msg = "**That item no longer exists — it may have been deleted.**"
    except AttentionItemAlreadyClosedError:
        msg = "**That item was already resolved or dismissed in another tab.**"
    except ValueError as exc:
        msg = f"**{exc}**"
    else:
        msg = ""
    df_update, cached_rows, detail_html = render_ar(
        request, type_filter, surgeon_filter, severity_filter, age_filter,
    )
    return (
        df_update, cached_rows, detail_html,
        gr.update(value=None), "", msg,
    )


def _admin_resolve_handler(
    request: gr.Request,
    item_id: int | None,
    on_behalf_of: str,
    reason: str,
    type_filter: str,
    surgeon_filter: str,
    severity_filter: str,
    age_filter: float,
):
    """Resolve-on-behalf-of handler. Mirrors :func:`_admin_dismiss_handler`
    plus the ``on_behalf_of`` field (the surgeon's username — picked
    from the affected_user of the currently-selected row when the panel
    populates, but the admin can override before clicking)."""
    scope = _scope_from_request(request)
    if scope is None or item_id is None:
        return (
            gr.update(), [], _empty_detail_html(),
            gr.update(value=None),
            "", "**Session expired — please sign in again.**",
        )
    stripped = (reason or "").strip()
    if len(stripped) < ADMIN_REASON_MIN_LENGTH:
        return (
            gr.update(), gr.update(), gr.update(), gr.update(),
            reason,
            f"**A reason of at least {ADMIN_REASON_MIN_LENGTH} "
            f"characters is required.**",
        )
    on_behalf = (on_behalf_of or "").strip() or None
    try:
        scope.repos.attention.admin_resolve(
            int(item_id), scope.username,
            reason=stripped, on_behalf_of=on_behalf,
        )
    except AttentionItemNotFoundError:
        msg = "**That item no longer exists — it may have been deleted.**"
    except AttentionItemAlreadyClosedError:
        msg = "**That item was already resolved or dismissed in another tab.**"
    except ValueError as exc:
        msg = f"**{exc}**"
    else:
        msg = ""
    df_update, cached_rows, detail_html = render_ar(
        request, type_filter, surgeon_filter, severity_filter, age_filter,
    )
    return (
        df_update, cached_rows, detail_html,
        gr.update(value=None), "", msg,
    )


# ===== top-level Blocks build =====


# Brief #3.1 / Brief #4 followup: no module-level CSS export. All
# styling lives inline in the rendered HTML chunks above. The
# previous ADMIN_CSS stylesheet — and ``css=`` on the mount — were
# stripped after the admin tabs hung on cv.digitalsurgeon.dev with
# the same Svelte ``effect_update_depth_exceeded`` cycle Brief
# #3.1 chased through My Cases. Inline styles sidestep the entire
# class of reactive-flush-meets-resize-observer problems.
ADMIN_CSS = ""


def build_admin_app() -> gr.Blocks:
    with gr.Blocks(
        title="Admin — surgical-cv",
        analytics_enabled=False,
    ) as blocks:
        identity_md = gr.Markdown()

        with gr.Tabs():
            with gr.Tab("Global Dashboard") as dashboard_tab:
                stat_strip = gr.HTML(_STAT_STRIP_TEMPLATE.format(
                    total_cases=0, intake_cases=0, open_ar=0, high_ar=0,
                    stale_cases=0,
                ))
                surgeon_df = gr.DataFrame(
                    headers=list(_PER_SURGEON_HEADERS),
                    datatype=_PER_SURGEON_DATATYPES,
                    interactive=False,
                    wrap=True,
                    elem_id="admin-surgeon-df",
                )

            with gr.Tab("Action Required") as ar_tab:
                # Filter row.
                with gr.Row():
                    type_filter = gr.Dropdown(
                        choices=[_ALL_TYPES_LABEL, "phi_redacted",
                                 "malformed_marker", "verify_soft_fail",
                                 "pipeline_failure", "orphan_marker"],
                        value=_ALL_TYPES_LABEL,
                        label="Type",
                    )
                    surgeon_filter = gr.Dropdown(
                        choices=[_ALL_SURGEONS_LABEL],
                        value=_ALL_SURGEONS_LABEL,
                        label="Surgeon",
                    )
                    severity_filter = gr.Dropdown(
                        choices=_SEVERITY_FILTER_OPTIONS,
                        value=_ALL_SEVERITIES_LABEL,
                        label="Severity",
                    )
                    age_filter = gr.Slider(
                        minimum=0, maximum=30, step=1, value=0,
                        label="Older than (days)",
                    )

                ar_df = gr.DataFrame(
                    headers=list(_AR_HEADERS),
                    datatype=_AR_DATATYPES,
                    interactive=False,
                    wrap=True,
                    elem_id="admin-ar-df",
                )
                cached_rows_state = gr.State([])
                selected_item_id = gr.State()
                detail_panel = gr.HTML(_empty_detail_html())

                with gr.Group():
                    action_error_md = gr.Markdown("")
                    reason_tb = gr.Textbox(
                        label="Reason (required, ≥ "
                              f"{ADMIN_REASON_MIN_LENGTH} characters)",
                        lines=2,
                    )
                    with gr.Row():
                        on_behalf_tb = gr.Textbox(
                            label="Resolve on behalf of (surgeon username)",
                            placeholder="e.g. asarin",
                        )
                    with gr.Row():
                        dismiss_btn = gr.Button(
                            "Dismiss", variant="secondary",
                        )
                        resolve_btn = gr.Button(
                            "Resolve on behalf", variant="primary",
                        )

        # ----- wiring -----

        def _populate_surgeon_filter() -> gr.update:
            users = _list_surgeon_users()
            choices = [_ALL_SURGEONS_LABEL] + [
                u["folder_slug"] for u in users
            ]
            return gr.update(choices=choices, value=_ALL_SURGEONS_LABEL)

        # Initial mount: identity + dashboard + AR table + surgeon filter
        # choices. blocks.load fires once on page open.
        blocks.load(_identity, None, identity_md)
        blocks.load(
            render_dashboard, None, [stat_strip, surgeon_df],
        )
        blocks.load(
            _populate_surgeon_filter, None, surgeon_filter,
        )
        blocks.load(
            render_ar,
            inputs=[type_filter, surgeon_filter, severity_filter, age_filter],
            outputs=[ar_df, cached_rows_state, detail_panel],
        )

        # Brief #4 followup: ``gr.Tab.select`` wiring removed. Pairing
        # ``tab.select`` with ``blocks.load`` on the same render
        # functions was the most plausible remaining suspect after the
        # custom CSS strip — multiple-event-source-into-same-output
        # is the wiring shape that historically tickles Svelte's
        # effect_update_depth in this codebase. ``blocks.load`` runs
        # both renders on initial mount; subsequent admin actions
        # (dismiss / resolve) refresh the AR table via their own
        # click handlers. The Dashboard tab stays at whatever values
        # were computed at page open until the user reloads — fine
        # for the admin's read-only summary use case.

        # Filter changes: re-run render_ar (every filter wires here).
        for component in (
            type_filter, surgeon_filter, severity_filter, age_filter,
        ):
            component.change(
                render_ar,
                inputs=[type_filter, surgeon_filter, severity_filter,
                        age_filter],
                outputs=[ar_df, cached_rows_state, detail_panel],
            )

        # Row select: populate detail panel + stash selected id.
        ar_df.select(
            _on_row_select,
            inputs=[cached_rows_state],
            outputs=[detail_panel, selected_item_id, reason_tb,
                     action_error_md],
        )

        # Action clicks: server-side reason check, repo call, then
        # re-render the table so the resolved row drops out of the open
        # view.
        dismiss_btn.click(
            _admin_dismiss_handler,
            inputs=[selected_item_id, reason_tb,
                    type_filter, surgeon_filter, severity_filter,
                    age_filter],
            outputs=[ar_df, cached_rows_state, detail_panel,
                     selected_item_id, reason_tb, action_error_md],
        )
        resolve_btn.click(
            _admin_resolve_handler,
            inputs=[selected_item_id, on_behalf_tb, reason_tb,
                    type_filter, surgeon_filter, severity_filter,
                    age_filter],
            outputs=[ar_df, cached_rows_state, detail_panel,
                     selected_item_id, reason_tb, action_error_md],
        )

    return blocks
