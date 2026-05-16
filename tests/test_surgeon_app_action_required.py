"""Integration tests for the Surgeon Action Required tab.

Three layers, mirroring the My Cases test file:

1. Blocks introspection — components present, slot count, timer cadence.
2. Direct render fn calls — exercise ``render_action_required`` and
   ``_ar_action_handler`` against a seeded SQLite DB with the full
   spectrum of attention_items types and severities.
3. Defense-in-depth coverage — UI bypass attempts (mismatched action,
   foreign user) hit the repo gates and are rejected without state
   mutation.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from app.auth import SESSION_COOKIE_NAME, encode_session
from app.db.connection import connect, utcnow
from app.repos.attention import (
    AttentionItemActionMismatchError,
    SqliteAttentionItemsRepository,
)


_SEED_TS = "2026-05-15T08:00:00+00:00"


# Brief #3.1.1 — AR render shape changed:
#   [0]  counter_md value
#   [1]  empty_html update
#   [2]  visible_attention_state value (list[dict])
#   per slot i, 3 outputs starting at 3 + i*3:
#     group_update, html_value, button_update
_AR_LEADING = 3
_AR_PER_SLOT = 3


def _ar_slot(out, i):
    base = _AR_LEADING + i * _AR_PER_SLOT
    return out[base: base + _AR_PER_SLOT]


def _fake_request_for(username: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: encode_session(username)}
    )


def _seed_attention(
    db_path,
    *,
    item_type: str,
    affected_user: str = "asarin",
    case_id: str = "UCD-FIL-001",
    severity: str = "normal",
    details: str = "test detail line",
    created_at: str = _SEED_TS,
) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.execute(
            "INSERT INTO attention_items "
            "(type, case_id, affected_user, severity, details, "
            " created_at, created_by, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (
                item_type, case_id, affected_user, severity, details,
                created_at, "asarin",
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _audit_rows(db_path) -> list[dict]:
    conn = connect(db_path)
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM admin_audit ORDER BY id DESC"
            ).fetchall()
        ]
    finally:
        conn.close()


# ----- 1. Blocks introspection -----


def test_action_required_tab_present_in_surgeon_blocks():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    labels = [c.label for c in blocks.blocks.values() if isinstance(c, gr.Tab)]
    assert "Action Required" in labels


def test_action_required_allocates_max_card_slots():
    """Pre-allocated card pool — the brief uses 10 as the comfortable
    upper bound."""
    from app.surgeon_app import _MAX_VISIBLE_ACTION_CARDS, build_surgeon_app

    assert _MAX_VISIBLE_ACTION_CARDS == 10
    blocks = build_surgeon_app()
    import gradio as gr
    slot_groups = [
        c for c in blocks.blocks.values()
        if isinstance(c, gr.Group)
        and (getattr(c, "elem_id", None) or "").startswith("ar-card-slot-")
    ]
    assert len(slot_groups) == _MAX_VISIBLE_ACTION_CARDS


def test_action_required_carries_per_slot_action_button():
    from app.surgeon_app import _MAX_VISIBLE_ACTION_CARDS, build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    btns = [
        c for c in blocks.blocks.values()
        if isinstance(c, gr.Button)
        and (getattr(c, "elem_id", None) or "").startswith("ar-card-btn-")
    ]
    assert len(btns) == _MAX_VISIBLE_ACTION_CARDS


def test_action_required_no_per_slot_id_states():
    """Brief #3.1.1 preemptive patch: the original Brief #3 AR pool
    allocated 2 ``gr.State`` per slot (``item_id_state`` +
    ``action_state``). At 10 slots that's 20 cascading state writes per
    render — same anti-pattern that broke My Cases at 50 slots, just
    below Svelte's flush threshold. The refactor moves to a single
    shared ``visible_attention_state`` list; this test guards against
    regressing to per-slot states."""
    from app.surgeon_app import _MAX_VISIBLE_ACTION_CARDS, build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    # No gr.State component should live inside an ar-card-slot-* group.
    # We approximate this by checking total state count is well below
    # the per-slot-state regression line.
    states = [c for c in blocks.blocks.values() if isinstance(c, gr.State)]
    # Per-slot AR states would add 20; the regression would have
    # ≥ Intake(13) + 20 = 33. Our target is Intake(13) + My Cases(2) +
    # AR(1 — visible_attention) = 16. Hard cap: 16.
    assert len(states) < 13 + 2 * _MAX_VISIBLE_ACTION_CARDS, (
        "per-slot AR state regression — slot pool should hold no "
        "gr.State instances (Brief #3.1.1 anti-pattern)"
    )


def test_action_required_has_30s_timer():
    """Same polling cadence as My Cases so the surgeon's mental model
    is uniform across tabs."""
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    timers = [c for c in blocks.blocks.values() if isinstance(c, gr.Timer)]
    # My Cases timer + Action Required timer.
    assert len(timers) == 2
    assert all(t.value == 30 for t in timers)


def test_action_required_has_counter_and_empty_components():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    md_ids = [
        getattr(c, "elem_id", None) for c in blocks.blocks.values()
        if isinstance(c, gr.Markdown)
    ]
    html_ids = [
        getattr(c, "elem_id", None) for c in blocks.blocks.values()
        if isinstance(c, gr.HTML)
    ]
    assert "ar-counter" in md_ids
    assert "ar-empty" in html_ids


# ----- 2. Direct render fn calls -----


def test_render_empty_state_for_user_with_no_items(app_env):
    """anoren has zero attention items → empty-state HTML visible, all
    card slots hidden."""
    from app.surgeon_app import (
        _MAX_VISIBLE_ACTION_CARDS, render_action_required,
    )
    out = render_action_required(_fake_request_for("anoren"))
    counter, empty_update = out[0], out[1]
    assert counter == "0 items · 0 resolved today · 0 pending"
    assert empty_update["visible"] is True
    # Walk the per-slot tuples (3 outputs each): all groups hidden.
    for i in range(_MAX_VISIBLE_ACTION_CARDS):
        slot = _ar_slot(out, i)
        group_update = slot[0]
        assert group_update["visible"] is False


def test_render_unauth_returns_empty_outputs(app_env):
    """No session → empty state, no crash. Defense in depth — production
    auth_dep gates /app/."""
    from app.surgeon_app import render_action_required
    out = render_action_required(types.SimpleNamespace(cookies={}))
    empty_update = out[1]
    assert empty_update["visible"] is True


def test_render_renders_one_card_per_open_item(app_env):
    """Seed three items of three different types; render_action_required
    surfaces all three with the correct labels and action buttons."""
    from app.surgeon_app import render_action_required

    _seed_attention(
        app_env, item_type="verify_soft_fail", severity="normal",
        details="qual flag",
    )
    _seed_attention(
        app_env, item_type="pipeline_failure", severity="high",
        details="pipeline boom",
    )
    _seed_attention(
        app_env, item_type="orphan_marker", severity="high",
        details="missing manifest",
    )

    out = render_action_required(_fake_request_for("asarin"))
    counter = out[0]
    assert "3 items" in counter
    assert "3 pending" in counter

    # Inspect the first three slot tuples + the shared
    # visible_attention_state list (Brief #3.1.1 surface).
    visible_attention = out[2]
    assert len(visible_attention) == 3
    visible = []
    for i in range(3):
        slot = _ar_slot(out, i)
        group_update, html_value, btn_update = slot
        if group_update.get("visible"):
            visible.append((html_value, btn_update, visible_attention[i]))
    assert len(visible) == 3

    # Type labels render in the cards.
    htmls = " ".join(v[0] for v in visible)
    assert "Quality flag" in htmls
    assert "Processing failed" in htmls
    assert "Incomplete submission" in htmls

    # Buttons label per dispatch map: dismiss for verify_soft_fail,
    # resolve for pipeline_failure and orphan_marker.
    actions = sorted(v[2]["action"] for v in visible)
    assert actions == ["dismiss", "resolve", "resolve"]


def test_render_filters_to_owned_items_only(app_env):
    """asarin sees only her items; anoren's items must NOT appear in
    her render. Cross-surgeon scope guard at the render layer."""
    from app.surgeon_app import render_action_required

    _seed_attention(
        app_env, item_type="verify_soft_fail", affected_user="asarin",
        details="mine",
    )
    _seed_attention(
        app_env, item_type="verify_soft_fail", affected_user="anoren",
        details="not mine",
    )

    out = render_action_required(_fake_request_for("asarin"))
    counter = out[0]
    assert "1 items" in counter
    htmls = " ".join(_ar_slot(out, i)[1] or "" for i in range(10))
    assert "mine" in htmls
    assert "not mine" not in htmls


def test_render_card_severity_maps_to_brand_class(app_env):
    """High-severity items get the .ds-card-severity-high stripe and
    .ds-badge-high pill; normal-severity items get the matching
    -normal classes."""
    from app.surgeon_app import render_action_required

    _seed_attention(
        app_env, item_type="pipeline_failure", severity="high",
    )

    out = render_action_required(_fake_request_for("asarin"))
    html_value = _ar_slot(out, 0)[1]
    assert "ds-card-severity-high" in html_value
    assert "ds-badge-high" in html_value


def test_render_unknown_type_renders_card_without_action_button(app_env):
    """Defensive: an unmapped attention_items.type still renders as a
    card so the surgeon sees that something needs reviewer attention,
    just without a clickable action."""
    from app.surgeon_app import render_action_required

    _seed_attention(
        app_env, item_type="some_future_type", severity="normal",
    )

    out = render_action_required(_fake_request_for("asarin"))
    slot = _ar_slot(out, 0)
    group_update, html_value, btn_update = slot
    assert group_update["visible"] is True
    assert btn_update["visible"] is False
    # Generic fallback label from display_for_type.
    assert "Some Future Type" in html_value
    # visible_attention_state carries the dispatch mapping; unmapped
    # type → empty action string.
    assert out[2][0]["action"] == ""


def test_render_html_contains_unescaped_markup(app_env):
    """Render-level guard: card HTML contains a literal ``<`` for an
    HTML tag, never the entity-encoded ``&lt;``. Same regression guard
    pattern Brief #2.5 added for the timeline SVG."""
    from app.surgeon_app import render_action_required

    _seed_attention(app_env, item_type="verify_soft_fail")
    out = render_action_required(_fake_request_for("asarin"))
    html_value = _ar_slot(out, 0)[1]
    assert "<article" in html_value
    assert "&lt;article" not in html_value


def test_render_counter_increments_resolved_today_after_dismiss(app_env):
    """resolved_today counter counts both dismiss and resolve actions
    by this surgeon since UTC midnight."""
    from app.surgeon_app import _ar_action_handler, render_action_required

    item_id = _seed_attention(app_env, item_type="verify_soft_fail")
    out = render_action_required(_fake_request_for("asarin"))
    assert "0 resolved today" in out[0]

    # Action handler returns a new render output tuple — counter
    # should now show 1 resolved today and zero pending.
    out2 = _ar_action_handler(
        item_id, "dismiss", _fake_request_for("asarin"),
    )
    assert "1 resolved today" in out2[0]
    assert "0 pending" in out2[0]


# ----- 3. Click handler defense-in-depth -----


def test_click_dismiss_on_verify_soft_fail_lands_audit(app_env):
    from app.surgeon_app import _ar_action_handler

    item_id = _seed_attention(app_env, item_type="verify_soft_fail")
    _ar_action_handler(item_id, "dismiss", _fake_request_for("asarin"))

    rows = _audit_rows(app_env)
    assert len(rows) == 1
    assert rows[0]["action"] == "attention.dismiss"
    assert rows[0]["target_id"] == str(item_id)
    assert rows[0]["admin_username"] == "asarin"


def test_click_resolve_on_pipeline_failure_lands_audit(app_env):
    from app.surgeon_app import _ar_action_handler

    item_id = _seed_attention(
        app_env, item_type="pipeline_failure", severity="high",
    )
    _ar_action_handler(item_id, "resolve", _fake_request_for("asarin"))

    rows = _audit_rows(app_env)
    assert len(rows) == 1
    assert rows[0]["action"] == "attention.resolve"
    assert rows[0]["target_id"] == str(item_id)


def test_action_handler_swallows_already_closed_race(app_env):
    """Two-tab double-click: first action succeeds, second is
    AttentionItemAlreadyClosedError. Handler swallows the race + just
    re-renders the live state — no exception propagated."""
    from app.surgeon_app import _ar_action_handler

    item_id = _seed_attention(app_env, item_type="verify_soft_fail")
    _ar_action_handler(item_id, "dismiss", _fake_request_for("asarin"))
    # Second click on the same already-dismissed item.
    out = _ar_action_handler(
        item_id, "dismiss", _fake_request_for("asarin"),
    )
    # Render returned cleanly; counter shows 1 resolved today (the
    # first action) and 0 pending.
    assert "1 resolved today" in out[0]
    assert "0 pending" in out[0]
    # Audit log shows the single successful action — the racing call
    # didn't write a phantom row.
    assert len(_audit_rows(app_env)) == 1


def test_action_handler_swallows_action_mismatch_via_ui_bypass(app_env):
    """If a malformed event somehow asks to resolve a verify_soft_fail
    (which is dismiss-only), the handler swallows the error and re-
    renders. The card remains open."""
    from app.surgeon_app import _ar_action_handler

    item_id = _seed_attention(app_env, item_type="verify_soft_fail")
    _ar_action_handler(item_id, "resolve", _fake_request_for("asarin"))
    # Item still open.
    rows = _audit_rows(app_env)
    assert rows == []


def test_repo_layer_blocks_dismiss_on_resolve_only_type_directly(app_env):
    """Regression guard against UI bypass: even via direct repo call,
    a dismiss on pipeline_failure raises AttentionItemActionMismatchError.
    Mirrors the brief's "regression guard against UI bypass" item."""
    item_id = _seed_attention(
        app_env, item_type="pipeline_failure", severity="high",
    )
    r = SqliteAttentionItemsRepository()
    with pytest.raises(AttentionItemActionMismatchError):
        r.dismiss(item_id, by="asarin")


# ----- empty state -----


def test_empty_state_html_contains_brand_class(app_env):
    from app.surgeon_app import _AR_EMPTY_HTML

    assert "ds-empty-state" in _AR_EMPTY_HTML
    assert "No action items of concern" in _AR_EMPTY_HTML


# ----- counter formatting -----


def test_counter_format_is_canonical(app_env):
    from app.surgeon_app import _format_ar_counter

    assert _format_ar_counter(3, 1, 3) == (
        "3 items · 1 resolved today · 3 pending"
    )
    assert _format_ar_counter(0, 0, 0) == (
        "0 items · 0 resolved today · 0 pending"
    )
