"""Tests for the My Cases tab in ``app/surgeon_app.py``.

Three layers:

1. **Blocks introspection** — confirm the tab construction wires the
   expected components (DataFrame, detail group, empty-state markdown,
   timer, footer). Doesn't exercise render fns.
2. **Direct render fn calls** — exercise ``render_my_cases`` and
   ``render_detail`` against in-memory fakes with explicit clocks.
3. **Integration via TestClient** — login through the FastAPI app, GET
   /app/, assert response shape (followed by a synthetic render call to
   check what would land on the page since Gradio renders client-side).
"""

from __future__ import annotations

import sqlite3
import time
import types

import pytest

from app.auth import (
    SESSION_COOKIE_NAME,
    encode_session,
)
from app.repos import (
    InMemoryAttentionItemsRepository,
    InMemoryCaseRepository,
    InMemoryPicklistRepository,
    InMemoryPipelineStateRepository,
    InMemoryRawSegmentRepository,
    Repos,
)
from pipeline.schemas import Stage
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
    """Mimic the gr.Request shape ``_scope_from_request`` reads from —
    just needs ``cookies`` with a session token."""
    return types.SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: encode_session(username)}
    )


def _seed_pipeline_state(monkeypatch, tmp_path, rows):
    """Write a tmp pipeline_state.csv and point PIPELINE_STATE_PATH at
    it. ``rows`` is a list of dicts with the canonical column names."""
    from pipeline.schemas import PIPELINE_STATE_COLUMNS
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


# ----- 1. Blocks introspection -----


def test_my_cases_tab_present_in_surgeon_blocks():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    labels = [c.label for c in blocks.blocks.values() if isinstance(c, gr.Tab)]
    assert "My Cases" in labels


def test_my_cases_blocks_carries_dataframe_with_status_column():
    from app.surgeon_app import _MY_CASES_DF_HEADERS, build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    dfs = [c for c in blocks.blocks.values() if isinstance(c, gr.DataFrame)]
    assert len(dfs) >= 1
    df = next(
        (d for d in dfs if list(d.headers) == _MY_CASES_DF_HEADERS),
        None,
    )
    assert df is not None, "My Cases DataFrame not found"
    assert "Status" in df.headers


def test_my_cases_columns_swap_or_for_indication():
    """Brief #2.5 column swap: OR is dropped from the DataFrame (it
    lives in the detail panel only); Indication takes its place."""
    from app.surgeon_app import _MY_CASES_DF_HEADERS

    assert _MY_CASES_DF_HEADERS == [
        "UCD-FIL-ID", "Date", "Procedure", "Approach", "Indication",
        "Status", "Updated",
    ]
    # Belt + suspenders: OR is gone, Indication is present.
    assert "OR" not in _MY_CASES_DF_HEADERS
    assert "Indication" in _MY_CASES_DF_HEADERS


def test_my_cases_status_column_uses_html_datatype():
    """Status column is per-column HTML so the badge ``<span>`` renders
    instead of escaping. Regression guard for the bug observed in
    production (badge HTML showing as literal ``&lt;span``)."""
    from app.surgeon_app import _MY_CASES_DF_DATATYPES, _MY_CASES_DF_HEADERS

    status_idx = _MY_CASES_DF_HEADERS.index("Status")
    assert _MY_CASES_DF_DATATYPES[status_idx] == "html"


def test_surgeon_css_constant_carries_brand_classes():
    """CSS is wired through ``gr.mount_gradio_app(css=...)`` (Gradio 6
    moved theme/css off gr.Blocks). The surgeon app exposes the CSS as
    a module attribute so app.main can pass it through. Regression
    guard: badge + SVG-timeline classes must be present so the My Cases
    tab renders branded pills + an unescaped SVG timeline."""
    from app.surgeon_app import SURGEON_CSS

    assert ".ds-badge" in SURGEON_CSS
    assert ".ds-timeline-svg" in SURGEON_CSS
    # Typography overrides also live in SURGEON_CSS — verify the
    # tab-label / identity rules made it in.
    assert "surgeon-identity" in SURGEON_CSS
    assert "Fraunces" in SURGEON_CSS


def test_surgeon_theme_uses_teal_primary_hue_not_orange():
    """Gradio default theme uses primary_hue=colors.orange. Brief #2.5
    swaps to teal so the active tab indicator + cell focus rings pick
    up brand colors instead of the system orange. The theme resolves
    the hue immediately into ``primary_*`` tokens, so we assert against
    the resolved 500-step color (teal-500 = #14b8a6, orange-500 = #f97316)."""
    from app.surgeon_app import SURGEON_THEME

    assert SURGEON_THEME.primary_500 == "#14b8a6"  # teal-500
    # Belt + suspenders: not orange-500.
    assert SURGEON_THEME.primary_500 != "#f97316"


def test_surgeon_app_main_mounts_with_theme_and_css():
    """Confirm the wiring at the FastAPI mount level — theme + css must
    be passed through gr.mount_gradio_app or the brand styling never
    reaches the browser. Catches a regression where the surgeon app is
    built in isolation but the mount call drops the theme/css kwargs."""
    import inspect

    import app.main as main_mod
    src = inspect.getsource(main_mod)
    # The surgeon mount block must include both theme= and css= kwargs.
    assert "theme=SURGEON_THEME" in src
    assert "css=SURGEON_CSS" in src


def test_my_cases_blocks_carries_30s_timer():
    """Brief #3 added the Action Required tab with its own 30 s timer,
    so the surgeon Blocks now carries two Timer components total. The
    My Cases timer is still 30 s — both tabs share the same polling
    cadence so the surgeon's mental model is uniform across them."""
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    timers = [c for c in blocks.blocks.values() if isinstance(c, gr.Timer)]
    assert len(timers) == 2
    assert all(t.value == 30 for t in timers)


def test_my_cases_blocks_has_detail_group_initially_hidden():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    groups = [c for c in blocks.blocks.values() if isinstance(c, gr.Group)]
    detail = next(
        (g for g in groups if getattr(g, "elem_id", None) == "my-cases-detail"),
        None,
    )
    assert detail is not None
    assert detail.visible is False


def test_my_cases_blocks_has_empty_state_and_footer():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    import gradio as gr
    md_components = [
        c for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    # Footer is identifiable by elem_id; empty state by initial value.
    footer = next(
        (m for m in md_components
         if getattr(m, "elem_id", None) == "my-cases-footer"),
        None,
    )
    assert footer is not None
    empty = next(
        (m for m in md_components
         if "No cases yet" in (m.value or "")),
        None,
    )
    assert empty is not None


# ----- 2. Direct render fn calls -----


def test_render_my_cases_with_no_cases_returns_empty_state(
    app_env, monkeypatch, tmp_path
):
    """A surgeon with zero owned cases gets the empty-state markdown
    visible, the dataframe hidden, the detail group hidden."""
    # anoren is seeded by conftest, has folder=noren which has no
    # manifest rows in the test fixture.
    from app.surgeon_app import render_my_cases

    out = render_my_cases(_fake_request_for("anoren"))
    assert len(out) == 5
    df_update, header, footer, empty_update, detail_update = out
    assert df_update["visible"] is False
    assert empty_update["visible"] is True
    assert "No cases yet" in str(empty_update["value"])
    assert detail_update["visible"] is False
    assert "Auto-refreshes every 30" in footer


def test_render_my_cases_with_owned_cases_returns_rows(
    app_env, monkeypatch, tmp_path
):
    """asarin owns UCD-FIL-001 and UCD-FIL-002 in conftest. Both have
    no pipeline_state row → both render as Queued."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])  # empty state CSV

    out = render_my_cases(_fake_request_for("asarin"))
    df_update, header, footer, empty_update, detail_update = out
    assert df_update["visible"] is True
    assert empty_update["visible"] is False
    rows = df_update["value"]
    case_ids = [r[0] for r in rows]
    assert "UCD-FIL-001" in case_ids
    assert "UCD-FIL-002" in case_ids
    # UCD-FIL-099 belongs to miller, must NOT appear.
    assert "UCD-FIL-099" not in case_ids
    # Header reflects the bucketing.
    assert "2 cases" in header
    assert "0 complete" in header
    assert "2 in progress" in header  # both are Queued → in-progress bucket


def test_render_my_cases_with_verified_state_shows_complete_badge(
    app_env, monkeypatch, tmp_path
):
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
    out = render_my_cases(_fake_request_for("asarin"))
    df_update, header, *_ = out
    rows = df_update["value"]
    # Status column is index 5; check the badge HTML shows complete and
    # carries the brand badge class so the cell renders as a pill rather
    # than escaped text.
    status_cells = [r[5] for r in rows]
    assert all('data-badge="complete"' in c for c in status_cells)
    assert all('class="ds-badge ds-badge-complete"' in c for c in status_cells)
    assert "2 complete" in header


def test_render_my_cases_unauth_returns_empty_state_gracefully():
    """No session → empty state, no crash. (Production auth_dep gates
    /app/ so this branch is unreachable from the browser; this is
    defense in depth for tests and direct invocations.)"""
    from app.surgeon_app import render_my_cases

    out = render_my_cases(types.SimpleNamespace(cookies={}))
    df_update, header, footer, empty_update, detail_update = out
    assert df_update["visible"] is False
    assert empty_update["visible"] is True


def test_render_detail_unauth_returns_blank_silently():
    from app.surgeon_app import render_detail

    evt = types.SimpleNamespace(row_value=["UCD-FIL-001"], index=[0, 0])
    out = render_detail(evt, types.SimpleNamespace(cookies={}))
    assert len(out) == 5
    timeline, metadata, segments, timestamps, group_update = out
    assert timeline == ""
    assert group_update["visible"] is False


def test_render_detail_for_unowned_case_returns_blank(
    app_env, monkeypatch, tmp_path
):
    """Defense in depth: even if a SelectData event somehow targets a
    case asarin doesn't own, the detail panel stays hidden and blank."""
    from app.surgeon_app import render_detail
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    # UCD-FIL-099 is owned by miller per conftest.
    evt = types.SimpleNamespace(row_value=["UCD-FIL-099"], index=[0, 0])
    out = render_detail(evt, _fake_request_for("asarin"))
    timeline, metadata, segments, timestamps, group_update = out
    assert timeline == ""
    assert metadata == ""
    assert group_update["visible"] is False


def test_render_detail_for_owned_case_renders_panel(
    app_env, monkeypatch, tmp_path
):
    from app.surgeon_app import render_detail
    _seed_pipeline_state(monkeypatch, tmp_path, [
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": ["seg-a.mp4", "seg-b.mp4"],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "concat_ts": "2026-05-12T08:30:00",
            "deid_ts": "2026-05-12T09:00:00",
            "verify_ts": "2026-05-12T09:30:00",
        },
    ])
    evt = types.SimpleNamespace(row_value=["UCD-FIL-001"], index=[0, 0])
    out = render_detail(evt, _fake_request_for("asarin"))
    timeline, metadata, segments, timestamps, group_update = out
    # Timeline is inline SVG (not div+span) — the brief acceptance asks
    # for an unescaped ``<svg`` in the rendered output. Regression guard
    # for the production bug where the timeline was rendering escaped.
    assert timeline.startswith("<svg")
    assert "ds-timeline-svg" in timeline
    assert "<text" in timeline  # step labels are <text> elements
    assert "Procedure" in metadata
    assert "seg-a.mp4" in segments
    assert "seg-b.mp4" in segments
    assert "intake:" in timestamps
    assert "verify:" in timestamps
    assert group_update["visible"] is True


def test_render_detail_emits_unescaped_svg(
    app_env, monkeypatch, tmp_path
):
    """Render-level integration: call render_detail and confirm the
    string going into the gr.HTML component contains a literal ``<svg``,
    not ``&lt;svg``. Catches the regression where html.escape (or a
    Markdown-component-as-output mistake) swallows the SVG markup."""
    from app.surgeon_app import render_detail
    _seed_pipeline_state(monkeypatch, tmp_path, [
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": ["a.mp4"],
            "stage": "verified",
            "intake_ts": "2026-05-12T08:00:00+00:00",
            "verify_ts": "2026-05-12T10:00:00",
        },
    ])
    evt = types.SimpleNamespace(row_value=["UCD-FIL-001"], index=[0, 0])
    timeline, *_ = render_detail(evt, _fake_request_for("asarin"))
    assert "<svg" in timeline
    assert "&lt;svg" not in timeline


def test_render_my_cases_emits_indication_not_or(
    app_env, monkeypatch, tmp_path
):
    """Verify the row builder pulls ``indication`` not ``or_room`` into
    column index 4 — Brief #2.5 column swap."""
    from app.surgeon_app import (
        _MY_CASES_DF_HEADERS, render_my_cases,
    )
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    out = render_my_cases(_fake_request_for("asarin"))
    df_update, *_ = out
    rows = df_update["value"]
    indication_idx = _MY_CASES_DF_HEADERS.index("Indication")
    # asarin's seeded cases use "Colorectal cancer" for indication.
    assert all(r[indication_idx] == "Colorectal cancer" for r in rows)
    # And the OR room ("OR 4") must NOT show up anywhere in the row —
    # it's intentionally absent from the table.
    for r in rows:
        assert "OR 4" not in r


def test_polling_render_yields_fresh_footer(app_env, monkeypatch, tmp_path):
    """Calling render_my_cases twice with a delay yields a different
    footer timestamp — the same fn the gr.Timer.tick wires."""
    from app.surgeon_app import render_my_cases
    _seed_pipeline_state(monkeypatch, tmp_path, [])

    first = render_my_cases(_fake_request_for("asarin"))
    time.sleep(1.05)  # > 1s so HH:MM:SS clock value differs
    second = render_my_cases(_fake_request_for("asarin"))
    assert first[2] != second[2], (
        "footer timestamp did not advance between two render calls"
    )


# ----- 3. Integration via TestClient -----


def test_app_get_returns_gradio_shell_for_authed_surgeon(
    client, monkeypatch
):
    """The Gradio shell loads — actual table rendering is client-side
    JS + websocket, so we don't assert UCD-FIL ids in this response.
    The render-fn coverage above exercises the data path."""
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/app/")
    assert r.status_code == 200
    assert "gradio" in r.text.lower()


def test_anoren_can_login_and_reach_my_cases(client, monkeypatch):
    """Second active surgeon (added in conftest) can sign in and reach
    the surgeon shell. Their My Cases tab will render the empty-state
    on the client; here we just confirm the shell mounts."""
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

    out = render_my_cases(_fake_request_for("anoren"))
    df_update, header, footer, empty_update, detail_update = out
    assert empty_update["visible"] is True
    assert df_update["visible"] is False
    # Belt + suspenders: serialized form of all outputs contains no asarin
    # case ids.
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
    """Pre-migration row (no intake_ts) → date column shows case_year."""
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
    """A row with intake_ts must sort above a row without, even if the
    legacy row has a newer case_year."""
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
