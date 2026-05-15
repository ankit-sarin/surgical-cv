"""Tests for the Intake tab's Section 3 (case context — case_year, or_room,
indication). Pure-Python coverage; the dynamically rendered dropdowns live
inside ``@gr.render`` and are covered by the uvicorn smoke."""

from __future__ import annotations

from types import SimpleNamespace

import gradio as gr
import pytest

from app.auth import SESSION_COOKIE_NAME, encode_session
from app.surgeon_app import (
    _OR_ROOM_MAX_LENGTH,
    _OR_ROOM_PLACEHOLDER,
    _PICKLIST_FIELDS_FOR_INTAKE,
    _normalize_or_room,
    build_surgeon_app,
    fetch_picklists,
)


def _make_request(token: str | None):
    cookies = {SESSION_COOKIE_NAME: token} if token else {}
    return SimpleNamespace(cookies=cookies, headers={})


# ----- fetch_picklists now returns all four intake fields -----


def test_fetch_picklists_includes_all_four_fields(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    assert set(data.keys()) == {"procedure", "approach", "case_year", "indication"}


def test_fetch_picklists_case_year_present(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    assert {p.value for p in data["case_year"]} == {"2026", "2025", "2024"}


def test_fetch_picklists_case_year_sort_order_matches_seed(app_env):
    """case_year is universal and DESC-sorted in production. Conftest seeds
    three rows with ascending sort_order (matching DESC year order)."""
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    values = [p.value for p in data["case_year"]]
    # sort_order ascending → 2026 (sort=10), 2025 (sort=20), 2024 (sort=30)
    assert values == ["2026", "2025", "2024"]


def test_fetch_picklists_indication_other_last(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    values = [p.value for p in data["indication"]]
    assert values[-1] == "Other"
    assert "Colorectal cancer" in values


def test_fetch_picklists_indication_sorted_by_sort_order(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    sort_orders = [p.sort_order for p in data["indication"]]
    assert sort_orders == sorted(sort_orders)


def test_fetch_picklists_unauthenticated_returns_all_four_empty(app_env):
    data = fetch_picklists(_make_request(token=None))
    assert data == {
        "procedure": [],
        "approach": [],
        "case_year": [],
        "indication": [],
    }


def test_fetch_picklists_admin_returns_all_four_empty(app_env):
    """Section 3 is surgeon-only (Intake tab) — admin gets the empty shape."""
    req = _make_request(token=encode_session("ankitsarin"))
    data = fetch_picklists(req)
    assert data == {
        "procedure": [],
        "approach": [],
        "case_year": [],
        "indication": [],
    }


# ----- _normalize_or_room trim + None semantics -----


def test_normalize_or_room_trims_whitespace():
    assert _normalize_or_room("  OR 4  ") == "OR 4"


def test_normalize_or_room_passes_internal_whitespace():
    assert _normalize_or_room("ASC OR 2") == "ASC OR 2"


def test_normalize_or_room_empty_returns_none():
    assert _normalize_or_room("") is None


def test_normalize_or_room_whitespace_only_returns_none():
    assert _normalize_or_room("   \t  ") is None


def test_normalize_or_room_none_returns_none():
    assert _normalize_or_room(None) is None


# ----- Blocks introspection -----


def test_intake_tab_carries_section3_header():
    blocks = build_surgeon_app()
    markdown_values = [
        c.value for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    assert any(v and "Section 3" in str(v) for v in markdown_values)


def test_intake_tab_carries_eleven_state_components():
    """Section 1 (3 states: segments, selected, show_more) +
    picklists_state +
    Section 2 (4 states: procedure_primary, procedure_additional,
    approach, conversion_target) +
    Section 3 (3 states: case_year, or_room, indication)
    = 11 gr.State components total."""
    blocks = build_surgeon_app()
    state_count = sum(
        1 for c in blocks.blocks.values() if isinstance(c, gr.State)
    )
    assert state_count >= 11


def test_intake_tab_carries_or_room_textbox():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    or_room_boxes = [t for t in textboxes if t.label == "OR room"]
    assert len(or_room_boxes) == 1


def test_or_room_textbox_has_placeholder():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    or_room = [t for t in textboxes if t.label == "OR room"][0]
    assert or_room.placeholder == _OR_ROOM_PLACEHOLDER


def test_or_room_textbox_has_max_length_50():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    or_room = [t for t in textboxes if t.label == "OR room"][0]
    assert or_room.max_length == _OR_ROOM_MAX_LENGTH
    assert _OR_ROOM_MAX_LENGTH == 50


def test_or_room_textbox_single_line():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    or_room = [t for t in textboxes if t.label == "OR room"][0]
    assert or_room.max_lines == 1


# ----- intake-form picklist field set -----


def test_picklist_fields_for_intake_is_the_four():
    assert _PICKLIST_FIELDS_FOR_INTAKE == (
        "procedure", "approach", "case_year", "indication",
    )
