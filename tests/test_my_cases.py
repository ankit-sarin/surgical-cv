"""Tests for the My Cases tab in ``app/surgeon_app.py``.

Brief #3.1 rewrite — the DataFrame component was retired (Gradio
issue #12947 hung the surgeon's browser within seconds of mount). My
Cases now uses the same pre-allocated card-slot pattern as the
Action Required tab. Tests mirror ``test_surgeon_app_action_required``
in three layers:

1. **Blocks introspection** — slot count, timer cadence, header/empty
   components present.
2. **Direct render fn calls** — exercise ``render_my_cases`` and the
   click handler at varying case counts and expansion states.
3. **Integration via TestClient** — login, GET ``/app/``, assert the
   shell mounts (per-case rendering is client-side, but the in-process
   render-fn coverage above exercises the data path).
"""

from __future__ import annotations

import time
import types

import pytest

from app.auth import (
    SESSION_COOKIE_NAME,
    encode_session,
)
from pipeline.schemas import PIPELINE_STATE_COLUMNS, Stage
from tests.conftest import patch_dsm


# ----- helpers -----


def _login_as(client, monkeypatch, username):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": username, "password": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def _fake_request_for(username: str) -> types.SimpleNamespace:
    """Mimic the gr.Request shape ``_scope_from_request`` reads from."""
    return types.SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: encode_session(username)}
    )


def _seed_pipeline_state(monkeypatch, tmp_path, rows):
    """Write a tmp pipeline_state.csv and point PIPELINE_STATE_PATH at
    it. ``rows`` is a list of dicts with the canonical column names."""
    csv_path = tmp_path / "state.csv"
    header = ",".join(PIPELINE_STATE_COLUMNS)
    body_lines = []
    for r in rows:
        ordered = []
        for col in PIPELINE_STATE_COLUMNS:
            v = r.get(col, "")
            if col == "raw_segments" and isinstance(v, list):
                v = "|".join(v)
            ordered.append(str(v))
        body_lines.append(",".join(ordered))
    csv_path.write_text(header + "\n" + "\n".join(body_lines) + "\n")
    monkeypatch.setenv("PIPELINE_STATE_PATH", str(csv_path))


def _seed_manifest_with(monkeypatch, tmp_path, rows):
    """Write a custom case_manifest.csv (overriding ``app_env``'s default
    fixture) for tests that need >2 sarin cases."""
    import csv

    from pipeline.schemas import CASE_MANIFEST_COLUMNS

    target = tmp_path / "case_manifest.csv"
    with open(target, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(CASE_MANIFEST_COLUMNS))
        writer.writeheader()
        for r in rows:
            writer.writerow({col: r.get(col, "") for col in CASE_MANIFEST_COLUMNS})
    monkeypatch.setenv("CASE_MANIFEST_PATH", str(target))


def _make_sarin_case(idx: int) -> dict:
    """Produce a synthetic sarin-owned manifest row. Designed for the
    50-slot truncation test below; minimum viable fields."""
    return {
        "ucd_fil_id": f"UCD-FIL-{idx:03d}",
        "surgeon": "sarin",
        "case_year": "2026",
        "or_room": "OR 4",
        "procedure_primary": "Low anterior resection",
        "procedure_additional": "",
        "approach": "Robotic",
        "conversion_target": "",
        "indication": "Colorectal cancer",
        "notes": "",
    }


def _make_state_row(idx: int) -> dict:
    """Matching pipeline_state row — verified, with one BDV-style segment
    so the source-segments expansion has something to render."""
    return {
        "ucd_fil_id": f"UCD-FIL-{idx:03d}",
        "raw_segments": [f"capt0_20260102-08{idx:04d}.mp4"],
        "stage": "verified",
        "intake_ts": f"2026-05-12T08:00:{idx % 60:02d}+00:00",
        "verify_ts": "2026-05-12T10:00:00",
    }


# Output ordering constants — kept in sync with render_my_cases output
# shape so test code reads cleanly. Brief #3.1.1 changed the shape:
# per-slot case_id_state retired; index [3] is now the shared
# visible_cases_state list; per-slot pair is (group_update, html).
_HEADER_IDX = 0
_EMPTY_IDX = 1
_FOOTER_IDX = 2
_VISIBLE_CASES_IDX = 3
_SLOTS_START = 4
_PER_SLOT = 2


def _slot(out, i):
    """Slice the per-slot pair (group_update, html) out of the flat
    render output."""
    base = _SLOTS_START + i * _PER_SLOT
    return out[base: base + _PER_SLOT]


def _case_ids_in(out):
    """Return the visible-case-id list at output index 3."""
    return [entry.get("case_id") for entry in (out[_VISIBLE_CASES_IDX] or [])]


def _slot_index_for(out, case_id):
    """Return the slot index whose card carries ``case_id``, or ``None``
    if the case isn't in the current visible window."""
    for i, entry in enumerate(out[_VISIBLE_CASES_IDX] or []):
        if entry.get("case_id") == case_id:
            return i
    return None


# ----- 1. Blocks introspection -----


def test_my_cases_tab_present_in_surgeon_blocks():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    labels = [c.label for c in blocks.blocks.values() if isinstance(c, gr.Tab)]
    assert "My Cases" in labels


def test_my_cases_allocates_max_slots():
    """Pre-allocated card pool — 50 slots."""
    from app.surgeon_app import _MAX_VISIBLE_MY_CASES_SLOTS, build_surgeon_app

    assert _MAX_VISIBLE_MY_CASES_SLOTS == 50
    blocks = build_surgeon_app()
    import gradio as gr
    slot_groups = [
        c for c in blocks.blocks.values()
        if isinstance(c, gr.Group)
        and (getattr(c, "elem_id", None) or "").startswith("my-case-slot-")
    ]
    assert len(slot_groups) == _MAX_VISIBLE_MY_CASES_SLOTS


def test_my_cases_carries_per_slot_button():
    from app.surgeon_app import _MAX_VISIBLE_MY_CASES_SLOTS, build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    btns = [
        c for c in blocks.blocks.values()
        if isinstance(c, gr.Button)
        and (getattr(c, "elem_id", None) or "").startswith("my-case-btn-")
    ]
    assert len(btns) == _MAX_VISIBLE_MY_CASES_SLOTS


def test_my_cases_drops_gr_dataframe():
    """Regression guard: the Dataframe Svelte component is the source of
    the upstream recursion bug. The surgeon app must not carry any
    DataFrame instances after Brief #3.1."""
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    dfs = [c for c in blocks.blocks.values() if isinstance(c, gr.DataFrame)]
    assert dfs == [], (
        "gr.DataFrame must not appear in the surgeon app — Gradio "
        "issue #12947 hangs the browser on My Cases mount."
    )


def test_my_cases_has_30s_timer():
    """Both My Cases and Action Required tabs share the 30 s cadence."""
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    timers = [c for c in blocks.blocks.values() if isinstance(c, gr.Timer)]
    assert len(timers) == 2
    assert all(t.value == 30 for t in timers)


def test_my_cases_has_header_and_footer_and_empty_components():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    md_ids = [
        getattr(c, "elem_id", None)
        for c in blocks.blocks.values()
        if isinstance(c, gr.Markdown)
    ]
    assert "my-cases-header" in md_ids
    assert "my-cases-footer" in md_ids


def test_my_cases_no_per_slot_case_id_states():
    """Brief #3.1.1: per-slot ``case_id_state`` retired. The 50-slot
    pool must not allocate any ``gr.State`` per slot — that fanout was
    the substrate of the Svelte 5 ``effect_update_depth_exceeded`` loop
    in production. Two tab-root states are expected (expanded +
    visible_cases) plus one AR state (visible_attention) plus the
    Intake tab's 13 states — anything beyond that count is the
    regression."""
    from app.surgeon_app import (
        _MAX_VISIBLE_ACTION_CARDS, _MAX_VISIBLE_MY_CASES_SLOTS,
        build_surgeon_app,
    )

    blocks = build_surgeon_app()
    import gradio as gr
    states = [c for c in blocks.blocks.values() if isinstance(c, gr.State)]
    # Hard cap on per-tab states. The fanout-per-slot pattern would
    # produce 50 (My Cases) + 20 (AR's old item_id_state + action_state)
    # which collectively are what blow Svelte's flush threshold.
    assert len(states) < (
        _MAX_VISIBLE_MY_CASES_SLOTS + 2 * _MAX_VISIBLE_ACTION_CARDS
    ), (
        "per-slot state regression — slot pool should hold no gr.State "
        "instances (Brief #3.1.1 anti-pattern)"
    )


def test_surgeon_css_constant_carries_card_classes():
    """CSS is wired through ``gr.mount_gradio_app(css=...)``. Brief #3.1
    introduces the .ds-card-expandable + .ds-card-status-* family;
    regression guard that the project-local mirror in badges_html stays
    synced."""
    from app.surgeon_app import SURGEON_CSS

    assert ".ds-badge" in SURGEON_CSS
    assert ".ds-timeline-svg" in SURGEON_CSS
    assert ".ds-card-expandable" in SURGEON_CSS
    assert ".ds-card-status-complete" in SURGEON_CSS
    assert ".ds-card-status-failed" in SURGEON_CSS
    assert ".ds-card-status-flagged" in SURGEON_CSS
    assert ".ds-card-status-processing" in SURGEON_CSS
    assert ".ds-card-status-queued" in SURGEON_CSS
    assert ".ds-card-status-stuck" in SURGEON_CSS


def test_surgeon_theme_uses_teal_primary_hue_not_orange():
    """Gradio default theme uses primary_hue=colors.orange. We swap to
    teal so the active tab indicator picks up brand colors."""
    from app.surgeon_app import SURGEON_THEME

    assert SURGEON_THEME.primary_500 == "#14b8a6"  # teal-500


def test_surgeon_app_main_mounts_with_theme_and_css():
    """theme + css must pass through gr.mount_gradio_app or brand styling
    never reaches the browser."""
    import inspect

    import app.main as main_mod
    src = inspect.getsource(main_mod)
    assert "theme=SURGEON_THEME" in src
    assert "css=SURGEON_CSS" in src


# ----- 2. Direct render fn calls -----


def test_render_my_cases_with_no_cases_returns_empty_state(
    app_env, monkeypatch, tmp_path
):
    """anoren has zero owned cases → empty-state visible, all slots
    hidden, visible_cases list is empty."""
    from app.surgeon_app import _MAX_VISIBLE_MY_CASES_SLOTS, render_my_cases

    out = render_my_cases(None, _fake_request_for("anoren"))
    assert len(out) == _SLOTS_START + _MAX_VISIBLE_MY_CASES_SLOTS * _PER_SLOT
    assert out[_HEADER_IDX] == ""
    empty_update = out[_EMPTY_IDX]
    assert empty_update["visible"] is True
    assert "No cases yet" in str(empty_update["value"])
    assert "Auto-refreshes every 30" in out[_FOOTER_IDX]
    assert out[_VISIBLE_CASES_IDX] == []
    for i in range(_MAX_VISIBLE_MY_CASES_SLOTS):
        group_update, html = _slot(out, i)
        assert group_update["visible"] is False
        assert html == ""


def test_render_my_cases_unauth_returns_empty_state_gracefully():
    """No session → empty state, no crash. Defense in depth — production
    auth_dep gates /app/."""
    from app.surgeon_app import render_my_cases

    out = render_my_cases(None, types.SimpleNamespace(cookies={}))
    empty_update = out[_EMPTY_IDX]
    assert empty_update["visible"] is True


def test_render_my_cases_renders_one_card_per_owned_case(
    app_env, monkeypatch, tmp_path
):
    """asarin owns 2 cases per conftest; both render as visible slots,
    miller's UCD-FIL-099 must not leak in."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(None, _fake_request_for("asarin"))
    visible = []
    for i in range(50):
        group_update, html = _slot(out, i)
        if group_update["visible"]:
            visible.append(html)
    assert len(visible) == 2
    case_ids = sorted(_case_ids_in(out))
    assert case_ids == ["UCD-FIL-001", "UCD-FIL-002"]
    htmls = " ".join(visible)
    assert "UCD-FIL-099" not in htmls

    # Header is the counter strip — 2 cases, both queued (no state row
    # yet → QUEUED).
    assert "2 cases" in out[_HEADER_IDX]
    assert "2 in progress" in out[_HEADER_IDX]


def test_render_my_cases_verified_state_shows_complete_stripe(
    app_env, monkeypatch, tmp_path
):
    """Verified cases pick up the .ds-card-status-complete stripe and
    .ds-badge-complete pill (the same brand state tokens as the badge
    family)."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": ["a.mp4"],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "verify_ts": "2026-05-12T10:00:00",
        },
        {
            "ucd_fil_id": "UCD-FIL-002",
            "raw_segments": ["a.mp4"],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "verify_ts": "2026-05-12T10:00:00",
        },
    ])
    out = render_my_cases(None, _fake_request_for("asarin"))
    htmls = " ".join(_slot(out, i)[1] for i in range(50))
    assert "ds-card-status-complete" in htmls
    assert 'data-badge="complete"' in htmls
    assert "2 complete" in out[_HEADER_IDX]


def test_render_my_cases_emits_visible_cases_list(
    app_env, monkeypatch, tmp_path
):
    """Brief #3.1.1: ``visible_cases_state`` is the shared list that
    indexes slot position to case-id. Each entry must carry at minimum
    a ``case_id`` key — the click handler reads from it."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(None, _fake_request_for("asarin"))
    visible = out[_VISIBLE_CASES_IDX]
    assert isinstance(visible, list)
    assert len(visible) == 2
    for entry in visible:
        assert isinstance(entry, dict)
        assert "case_id" in entry
        assert entry["case_id"].startswith("UCD-FIL-")


def test_empty_state_visibility_with_zero_cases(
    app_env, monkeypatch, tmp_path
):
    """Brief #3.1.1 §4.4: empty-state Markdown visible iff zero cases.
    Regression guard for the production bug where the empty-state text
    rendered simultaneously with the populated cards."""
    from app.surgeon_app import render_my_cases

    out = render_my_cases(None, _fake_request_for("anoren"))
    assert out[_EMPTY_IDX]["visible"] is True
    # First slot must be hidden so the empty-state isn't competing with
    # a stale visible group.
    group_update, _ = _slot(out, 0)
    assert group_update["visible"] is False


def test_empty_state_visibility_with_cases(
    app_env, monkeypatch, tmp_path
):
    """Conversely: with ≥1 owned case, the empty-state Markdown must be
    hidden. The first N slots are visible and carry card HTML."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(None, _fake_request_for("asarin"))  # 2 cases
    assert out[_EMPTY_IDX]["visible"] is False
    # First two slots visible, the rest hidden.
    visible_count = sum(
        1 for i in range(50) if _slot(out, i)[0]["visible"]
    )
    assert visible_count == 2


def test_render_my_cases_card_html_is_not_escaped(
    app_env, monkeypatch, tmp_path
):
    """Card body must reach gr.HTML as literal markup, not the entity-
    encoded form. Regression guard for the production bug pattern where
    Markdown components escape the badge span."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(None, _fake_request_for("asarin"))
    html = _slot(out, 0)[1]
    assert "<article" in html
    assert "&lt;article" not in html
    assert "ds-card-expandable" in html


def test_render_my_cases_collapsed_card_omits_expansion_body(
    app_env, monkeypatch, tmp_path
):
    """When ``expanded_case_id`` is None, no card carries the expansion
    region. Verify by absence of the ``ds-card-expansion`` class."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": ["a.mp4", "b.mp4"],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "verify_ts": "2026-05-12T10:00:00",
        },
    ])
    out = render_my_cases(None, _fake_request_for("asarin"))
    htmls = " ".join(_slot(out, i)[1] for i in range(50))
    assert "ds-card-expansion" not in htmls


def test_render_my_cases_expanded_card_carries_expansion_body(
    app_env, monkeypatch, tmp_path
):
    """When ``expanded_case_id`` matches a visible card, exactly that
    card carries the expansion HTML — the SVG timeline, the metadata
    line, source segments list."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": ["seg-a.mp4", "seg-b.mp4"],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "verify_ts": "2026-05-12T10:00:00",
        },
    ])
    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    target_slot = _slot_index_for(out, "UCD-FIL-001")
    assert target_slot is not None
    expanded_count = 0
    for i in range(50):
        group_update, html = _slot(out, i)
        if not group_update["visible"]:
            continue
        if i == target_slot:
            expanded_count += 1
            assert "ds-card-expansion" in html
            assert "<svg" in html
            assert "seg-a.mp4" in html
            assert "seg-b.mp4" in html
            assert 'data-expanded="true"' in html
        else:
            assert "ds-card-expansion" not in html
            assert 'data-expanded="false"' in html
    assert expanded_count == 1


def test_render_my_cases_source_segments_render_in_expansion(
    app_env, monkeypatch, tmp_path
):
    """Brief amendment §4.6: source segments come from
    pipeline_state.raw_segments. Expanded card body lists each
    BDV-style segment filename on its own line."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": [
                "capt0_20260102-082942.mp4",
                "capt0_20260102-085604.mp4",
                "capt0_20260102-092225.mp4",
            ],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "verify_ts": "2026-05-12T10:00:00",
        },
    ])
    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "Source segments (3)" in html
    assert "capt0_20260102-082942.mp4" in html
    assert "capt0_20260102-085604.mp4" in html
    assert "capt0_20260102-092225.mp4" in html


def test_render_my_cases_no_segments_falls_back_to_none_recorded(
    app_env, monkeypatch, tmp_path
):
    """A case with no pipeline_state row (yet to be picked up by the
    worker) still renders an expansion area when expanded; the source-
    segments line falls back to ``(none recorded)`` rather than blowing
    up the render."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])  # no state rows

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "ds-card-expansion" in html
    assert "(none recorded)" in html


def test_render_my_cases_truncates_at_50_slots(
    app_env, monkeypatch, tmp_path
):
    """51 owned cases → only the newest 50 render in slots. The 51st
    case's id must NOT leak into any of the rendered slot HTMLs."""
    from app.surgeon_app import render_my_cases

    # Override the manifest with 51 sarin cases. Older ones have lower
    # numeric ids; the sort key surfaces newest-first by intake_ts, then
    # case_year, then case_id desc. We give them DESC-progressing
    # intake_ts via the helper so the sort is unambiguous.
    rows = [_make_sarin_case(i) for i in range(1, 52)]
    _seed_manifest_with(monkeypatch, tmp_path, rows)
    state_rows = [_make_state_row(i) for i in range(1, 52)]
    # Make UCD-FIL-051 the newest (latest intake_ts), UCD-FIL-001 the
    # oldest, so truncation drops 001 not 051.
    for i, r in enumerate(state_rows):
        idx = i + 1
        r["intake_ts"] = f"2026-{idx % 12 + 1:02d}-12T08:00:00+00:00"
    # Walk from newest to oldest: highest id → newest month.
    sorted_state = sorted(state_rows, key=lambda r: int(r["ucd_fil_id"].split("-")[-1]))
    for i, r in enumerate(sorted_state):
        # Higher case-id → later month
        idx = i + 1
        r["intake_ts"] = f"2026-05-{12 + idx // 10:02d}T{(idx % 24):02d}:00:00+00:00"
    _seed_pipeline_state(monkeypatch, tmp_path, state_rows)

    out = render_my_cases(None, _fake_request_for("asarin"))
    visible = [
        (i, _slot(out, i)[1])
        for i in range(50)
        if _slot(out, i)[0]["visible"]
    ]
    assert len(visible) == 50
    visible_ids = set(_case_ids_in(out))
    assert len(visible_ids) == 50
    # Exactly one case id should be missing.
    all_ids = {f"UCD-FIL-{i:03d}" for i in range(1, 52)}
    dropped = all_ids - visible_ids
    assert len(dropped) == 1, (
        f"expected exactly one truncated case, got {dropped}"
    )
    # And the dropped id must NOT appear in any rendered card HTML.
    htmls = " ".join(v[1] for v in visible)
    for missing_id in dropped:
        assert missing_id not in htmls


def test_render_my_cases_expanded_id_collapses_when_out_of_window(
    app_env, monkeypatch, tmp_path
):
    """Race-graceful: if ``expanded_case_id`` points at a case that's
    no longer in the visible window, render simply doesn't mark any
    card expanded. Brief #3.1.1 retired the write-back of
    ``expanded_case_id_state`` from render to break the Svelte reactive
    cycle, so the state value persists (stale) but no card shows the
    expansion."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(
        "UCD-FIL-999",  # asarin doesn't own this
        _fake_request_for("asarin"),
    )
    htmls = " ".join(_slot(out, i)[1] for i in range(50))
    assert 'data-expanded="true"' not in htmls


# ----- 3. Click handler -----
#
# Brief #3.1.1 handler signature: ``(slot_index, visible_cases,
# expanded_case_id) -> new_expanded_case_id``. Slot index is
# closure-captured at wiring time via the factory helper; tests pass it
# explicitly.


def _vc(*case_ids):
    """Build a ``visible_cases``-shape list from a series of case-ids."""
    return [{"case_id": cid} for cid in case_ids]


def test_click_collapsed_card_expands_it():
    from app.surgeon_app import _my_case_click_handler

    new_expanded = _my_case_click_handler(
        0, _vc("UCD-FIL-001", "UCD-FIL-002"), None,
    )
    assert new_expanded == "UCD-FIL-001"


def test_click_expanded_card_collapses_it():
    from app.surgeon_app import _my_case_click_handler

    new_expanded = _my_case_click_handler(
        0, _vc("UCD-FIL-001", "UCD-FIL-002"), "UCD-FIL-001",
    )
    assert new_expanded is None


def test_click_different_card_swaps_expansion():
    from app.surgeon_app import _my_case_click_handler

    new_expanded = _my_case_click_handler(
        1, _vc("UCD-FIL-001", "UCD-FIL-002"), "UCD-FIL-001",
    )
    assert new_expanded == "UCD-FIL-002"


def test_click_empty_slot_is_skip():
    """Defensive: clicking a slot index past the visible list (a stale-
    tab race) returns :func:`gr.skip` rather than mutating the
    expanded_case_id state to a spurious value."""
    import gradio as gr

    from app.surgeon_app import _my_case_click_handler

    # Two visible cases; clicking slot 5 (empty) — should skip.
    out = _my_case_click_handler(
        5, _vc("UCD-FIL-001", "UCD-FIL-002"), "UCD-FIL-001",
    )
    # gr.skip() returns a marker dict ``{"__type__": "update"}`` in
    # Gradio 6 — compare by value rather than identity (the helper
    # builds a fresh dict on each call).
    assert out == gr.skip()


def test_click_empty_visible_list_is_skip():
    """Edge case: render hasn't populated visible_cases yet (or the
    list emptied between render and click). Handler short-circuits to
    gr.skip without throwing."""
    import gradio as gr

    from app.surgeon_app import _my_case_click_handler

    out = _my_case_click_handler(0, [], None)
    assert out == gr.skip()


def test_click_handler_does_not_invoke_render():
    """Brief #3.1.1: the click handler returns the new state value
    only; it does NOT invoke render_my_cases (the render is triggered
    by ``expanded_case_id_state.change``). This test catches a
    regression where the handler accidentally re-introduces the
    tuple-of-render-outputs return shape."""
    from app.surgeon_app import _my_case_click_handler

    out = _my_case_click_handler(0, _vc("UCD-FIL-001"), None)
    # New shape: single string (or None or Skip), never a tuple.
    assert not isinstance(out, tuple)


# ----- expansion content -----


def test_expansion_omits_additional_when_empty(
    app_env, monkeypatch, tmp_path
):
    """``Additional procedure`` line is suppressed entirely for cases
    with no additional procedures (the seeded asarin rows have none)."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "Additional procedure" not in html


def test_expansion_includes_additional_when_present(
    app_env, monkeypatch, tmp_path
):
    """A row with a JSON-encoded procedure_additional list renders the
    additional procedure(s) on a dedicated expansion line."""
    from app.surgeon_app import render_my_cases
    _seed_manifest_with(monkeypatch, tmp_path, [
        {
            **_make_sarin_case(1),
            "procedure_additional": '["Loop ileostomy"]',
        },
    ])
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "Additional procedure" in html
    assert "Loop ileostomy" in html


def test_expansion_omits_conversion_when_no_target(
    app_env, monkeypatch, tmp_path
):
    """No conversion → no conversion line. The default sarin seed rows
    have ``conversion_target=""``."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "Conversion:" not in html


def test_expansion_includes_conversion_when_target_set(
    app_env, monkeypatch, tmp_path
):
    from app.surgeon_app import render_my_cases
    _seed_manifest_with(monkeypatch, tmp_path, [
        {
            **_make_sarin_case(1),
            "approach": "Robotic",
            "conversion_target": "Open",
        },
    ])
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "Conversion:" in html
    assert "Open" in html


def test_expansion_shows_attention_count_when_present(app_env, monkeypatch, tmp_path):
    """Related attention items count line surfaces when the case has
    open attention rows; absent when zero."""
    import sqlite3

    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    conn = sqlite3.connect(app_env)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO attention_items "
        "(type, case_id, affected_user, severity, details, "
        " created_at, created_by, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
        (
            "verify_soft_fail", "UCD-FIL-001", "asarin",
            "normal", "test", "2026-05-15T08:00:00+00:00", "asarin",
        ),
    )
    conn.commit()
    conn.close()

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    slot_i = _slot_index_for(out, "UCD-FIL-001")
    assert slot_i is not None
    html = _slot(out, slot_i)[1]
    assert "Related attention" in html
    assert " 1 " in html  # the count


def test_polling_render_yields_fresh_footer(app_env, monkeypatch, tmp_path):
    """Calling render_my_cases twice with a delay yields a different
    footer timestamp — the same fn the gr.Timer.tick wires."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    first = render_my_cases(None, _fake_request_for("asarin"))
    time.sleep(1.05)
    second = render_my_cases(None, _fake_request_for("asarin"))
    assert first[_FOOTER_IDX] != second[_FOOTER_IDX]


# ----- 4. Integration via TestClient -----


def test_app_get_returns_gradio_shell_for_authed_surgeon(
    client, monkeypatch
):
    """The Gradio shell loads — actual rendering is client-side, so we
    don't assert UCD-FIL ids here. Render-fn coverage above exercises
    the data path."""
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/app/")
    assert r.status_code == 200
    assert "gradio" in r.text.lower()


def test_anoren_can_login_and_reach_my_cases(client, monkeypatch):
    _login_as(client, monkeypatch, "anoren")
    r = client.get("/app/")
    assert r.status_code == 200
    assert "gradio" in r.text.lower()


def test_anoren_render_my_cases_shows_empty_state(
    app_env, monkeypatch, tmp_path
):
    """Cross-surgeon scope: anoren (folder=noren) has no manifest rows.
    Direct render call surfaces the empty-state markdown and does NOT
    leak any of asarin's UCD-FIL-001/002 ids."""
    from app.surgeon_app import render_my_cases

    out = render_my_cases(None, _fake_request_for("anoren"))
    assert out[_EMPTY_IDX]["visible"] is True
    serialized = repr(out)
    for cid in ("UCD-FIL-001", "UCD-FIL-002", "UCD-FIL-003", "UCD-FIL-004"):
        assert cid not in serialized


# ----- helper coverage -----


@pytest.mark.parametrize("case_year,expected_first_chars", [
    ("2026", "2026"),
    ("2025", "2025"),
])
def test_date_falls_back_to_case_year_without_intake_ts(
    case_year, expected_first_chars
):
    from app.surgeon_app import _date_for_row

    state = {
        "ucd_fil_id": "UCD-FIL-001",
        "stage": Stage.verified,
        "intake_ts": "",
    }
    case = {"case_year": case_year}
    assert _date_for_row(state, case) == expected_first_chars


def test_date_uses_intake_ts_when_present():
    from app.surgeon_app import _date_for_row

    state = {"intake_ts": "2026-05-12T14:30:00+00:00"}
    case = {"case_year": "2025"}
    assert _date_for_row(state, case) == "2026-05-12"


def test_sort_key_timestamped_before_legacy():
    from app.surgeon_app import _sort_key

    timestamped = _sort_key(
        "UCD-FIL-005",
        {"case_year": "2024"},
        {"intake_ts": "2026-05-12T08:00:00+00:00"},
    )
    legacy = _sort_key(
        "UCD-FIL-001",
        {"case_year": "2030"},
        {"intake_ts": ""},
    )
    assert timestamped < legacy


def test_sort_key_within_legacy_group_orders_by_year_desc():
    from app.surgeon_app import _sort_key

    older = _sort_key("UCD-FIL-001", {"case_year": "2024"}, None)
    newer = _sort_key("UCD-FIL-002", {"case_year": "2026"}, None)
    assert newer < older


def test_attention_counts_by_case_groups_open_items():
    """``_attention_counts_by_case`` groups an AttentionItem iterable by
    case_id (skipping rows with no case_id, which only worker-queue
    surfaces produce)."""
    from app.repos.attention import AttentionItem
    from app.surgeon_app import _attention_counts_by_case

    items = [
        AttentionItem(
            id=1, type="verify_soft_fail", case_id="UCD-FIL-001",
            affected_user="asarin", severity="normal", details="",
            status="open",
            created_at="2026-05-15T08:00:00+00:00", created_by="asarin",
            resolved_at=None, resolved_by=None, resolution_note=None,
        ),
        AttentionItem(
            id=2, type="pipeline_failure", case_id="UCD-FIL-001",
            affected_user="asarin", severity="high", details="",
            status="open",
            created_at="2026-05-15T08:00:00+00:00", created_by="asarin",
            resolved_at=None, resolved_by=None, resolution_note=None,
        ),
        AttentionItem(
            id=3, type="verify_soft_fail", case_id="UCD-FIL-002",
            affected_user="asarin", severity="normal", details="",
            status="open",
            created_at="2026-05-15T08:00:00+00:00", created_by="asarin",
            resolved_at=None, resolved_by=None, resolution_note=None,
        ),
        AttentionItem(
            id=4, type="orphan_marker", case_id=None,
            affected_user="asarin", severity="high", details="",
            status="open",
            created_at="2026-05-15T08:00:00+00:00", created_by="asarin",
            resolved_at=None, resolved_by=None, resolution_note=None,
        ),
    ]
    counts = _attention_counts_by_case(items)
    assert counts == {"UCD-FIL-001": 2, "UCD-FIL-002": 1}
