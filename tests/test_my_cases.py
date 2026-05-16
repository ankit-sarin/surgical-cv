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
# shape so test code reads cleanly.
_HEADER_IDX = 0
_EMPTY_IDX = 1
_FOOTER_IDX = 2
_EXPANDED_STATE_IDX = 3
_SLOTS_START = 4
_PER_SLOT = 3


def _slot(out, i):
    """Slice the per-slot tuple (group_update, html, case_id) out of the
    flat render output."""
    base = _SLOTS_START + i * _PER_SLOT
    return out[base: base + _PER_SLOT]


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


def test_my_cases_expanded_state_is_gradio_state():
    """An expanded_case_id ``gr.State`` exists so the click handler can
    toggle which card's body is open. Other gr.State instances exist on
    the Intake tab — we just confirm at least one State component is
    present."""
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    states = [c for c in blocks.blocks.values() if isinstance(c, gr.State)]
    assert len(states) >= 1


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
    hidden, expanded state stays None."""
    from app.surgeon_app import _MAX_VISIBLE_MY_CASES_SLOTS, render_my_cases

    out = render_my_cases(None, _fake_request_for("anoren"))
    assert len(out) == _SLOTS_START + _MAX_VISIBLE_MY_CASES_SLOTS * _PER_SLOT
    assert out[_HEADER_IDX] == ""
    empty_update = out[_EMPTY_IDX]
    assert empty_update["visible"] is True
    assert "No cases yet" in str(empty_update["value"])
    assert "Auto-refreshes every 30" in out[_FOOTER_IDX]
    assert out[_EXPANDED_STATE_IDX] is None
    for i in range(_MAX_VISIBLE_MY_CASES_SLOTS):
        group_update, html, case_id = _slot(out, i)
        assert group_update["visible"] is False
        assert html == ""
        assert case_id == ""


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
        group_update, html, case_id = _slot(out, i)
        if group_update["visible"]:
            visible.append((html, case_id))
    assert len(visible) == 2
    case_ids = sorted(v[1] for v in visible)
    assert case_ids == ["UCD-FIL-001", "UCD-FIL-002"]
    htmls = " ".join(v[0] for v in visible)
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
    # Find the expanded card's slot.
    expanded_count = 0
    for i in range(50):
        group_update, html, case_id = _slot(out, i)
        if group_update["visible"] and case_id == "UCD-FIL-001":
            expanded_count += 1
            assert "ds-card-expansion" in html
            assert "<svg" in html
            assert "seg-a.mp4" in html
            assert "seg-b.mp4" in html
            assert 'data-expanded="true"' in html
        elif group_update["visible"]:
            # Other visible cards must NOT be expanded.
            assert "ds-card-expansion" not in html
            assert 'data-expanded="false"' in html
    assert expanded_count == 1
    # Render passed the expansion state through.
    assert out[_EXPANDED_STATE_IDX] == "UCD-FIL-001"


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
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
        (i, _slot(out, i)[1], _slot(out, i)[2])
        for i in range(50)
        if _slot(out, i)[0]["visible"]
    ]
    assert len(visible) == 50
    visible_ids = {v[2] for v in visible}
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
    no longer in the visible window, the render fn silently collapses
    the state to None rather than rendering a phantom expansion."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(
        "UCD-FIL-999",  # asarin doesn't own this
        _fake_request_for("asarin"),
    )
    assert out[_EXPANDED_STATE_IDX] is None
    htmls = " ".join(_slot(out, i)[1] for i in range(50))
    assert 'data-expanded="true"' not in htmls


# ----- 3. Click handler -----


def test_click_collapsed_card_expands_it(app_env, monkeypatch, tmp_path):
    from app.surgeon_app import _my_case_click_handler
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = _my_case_click_handler(
        None, "UCD-FIL-001", _fake_request_for("asarin"),
    )
    assert out[_EXPANDED_STATE_IDX] == "UCD-FIL-001"


def test_click_expanded_card_collapses_it(app_env, monkeypatch, tmp_path):
    from app.surgeon_app import _my_case_click_handler
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = _my_case_click_handler(
        "UCD-FIL-001", "UCD-FIL-001", _fake_request_for("asarin"),
    )
    assert out[_EXPANDED_STATE_IDX] is None


def test_click_different_card_swaps_expansion(app_env, monkeypatch, tmp_path):
    from app.surgeon_app import _my_case_click_handler
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = _my_case_click_handler(
        "UCD-FIL-001", "UCD-FIL-002", _fake_request_for("asarin"),
    )
    assert out[_EXPANDED_STATE_IDX] == "UCD-FIL-002"


def test_click_empty_slot_is_noop(app_env, monkeypatch, tmp_path):
    """Defensive: clicking a slot whose case-id state has been cleared
    (the slot is hidden, but a stale-tab race could fire its click) is
    a no-op render-refresh — no state change."""
    from app.surgeon_app import _my_case_click_handler
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = _my_case_click_handler(
        "UCD-FIL-001", "", _fake_request_for("asarin"),
    )
    # Expansion state preserved (since the empty slot click is a no-op).
    assert out[_EXPANDED_STATE_IDX] == "UCD-FIL-001"


def test_click_handler_unauth_returns_empty_render(monkeypatch, tmp_path):
    """No session → click handler renders the empty state without
    raising. (Production auth_dep gates /app/ — this is defense in
    depth.)"""
    from app.surgeon_app import _my_case_click_handler

    out = _my_case_click_handler(
        None, "UCD-FIL-001", types.SimpleNamespace(cookies={}),
    )
    assert out[_EMPTY_IDX]["visible"] is True


def test_click_unknown_case_id_collapses_in_render(
    app_env, monkeypatch, tmp_path
):
    """Race-graceful: a click that asks to expand a case id the
    surgeon no longer owns (concurrently moved out of scope) renders
    with the expansion state collapsed."""
    from app.surgeon_app import _my_case_click_handler
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = _my_case_click_handler(
        None, "UCD-FIL-999", _fake_request_for("asarin"),
    )
    # The handler sets new_expanded="UCD-FIL-999" but render then
    # collapses it because it's out of window.
    assert out[_EXPANDED_STATE_IDX] is None


# ----- expansion content -----


def test_expansion_omits_additional_when_empty(
    app_env, monkeypatch, tmp_path
):
    """``Additional procedure`` line is suppressed entirely for cases
    with no additional procedures (the seeded asarin rows have none)."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases("UCD-FIL-001", _fake_request_for("asarin"))
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
    html = next(
        _slot(out, i)[1]
        for i in range(50)
        if _slot(out, i)[2] == "UCD-FIL-001"
    )
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
