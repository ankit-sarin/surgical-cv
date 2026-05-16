"""Tests for Section 5 (submit handler integration) in ``app/surgeon_app.py``.

The bulk of the integration logic lives in ``app/intake/submit.py`` and is
covered in test_intake_validation.py / test_submit_case_repo.py. This file
sticks to Blocks-shape introspection plus a handful of integration smokes
that bypass Gradio's event loop and call the handler-bound helpers directly."""

from __future__ import annotations

import gradio as gr
import pytest

from app.surgeon_app import (
    _CLEAR_FORM_BANNER,
    _format_success_banner,
    build_surgeon_app,
)


# ----- Blocks introspection -----


def test_section5_header_present():
    blocks = build_surgeon_app()
    markdown_values = [
        c.value for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    assert any(v and "Section 5" in str(v) for v in markdown_values)


def test_submit_button_present():
    blocks = build_surgeon_app()
    buttons = [c for c in blocks.blocks.values() if isinstance(c, gr.Button)]
    submit_btns = [b for b in buttons if b.value == "Submit case"]
    assert len(submit_btns) == 1
    assert submit_btns[0].variant == "primary"


def test_clear_form_button_present():
    blocks = build_surgeon_app()
    buttons = [c for c in blocks.blocks.values() if isinstance(c, gr.Button)]
    clear_btns = [b for b in buttons if b.value == "Clear form"]
    assert len(clear_btns) == 1


def test_clear_form_button_secondary_variant():
    """Visually less prominent than the Submit button per spec."""
    blocks = build_surgeon_app()
    buttons = [c for c in blocks.blocks.values() if isinstance(c, gr.Button)]
    clear_btn = [b for b in buttons if b.value == "Clear form"][0]
    assert clear_btn.variant == "secondary"


def test_phi_confirm_buttons_present():
    blocks = build_surgeon_app()
    buttons = [c for c in blocks.blocks.values() if isinstance(c, gr.Button)]
    confirm = [b for b in buttons if b.value == "Confirm and submit"]
    cancel = [b for b in buttons if b.value == "Cancel"]
    assert len(confirm) == 1
    assert len(cancel) == 1
    assert confirm[0].variant == "primary"


def test_phi_confirm_group_hidden_initially():
    """Spec: gr.Group hidden by default, revealed at the PHI gate."""
    blocks = build_surgeon_app()
    groups = [c for c in blocks.blocks.values() if isinstance(c, gr.Group)]
    # At least one group with visible=False (the PHI confirm dialog).
    hidden_groups = [g for g in groups if not g.visible]
    assert len(hidden_groups) >= 1


def test_intake_tab_state_count_unchanged_at_thirteen():
    """Section 5 doesn't add data states — the 13-state total from Spec H
    for the Intake tab must hold (show_more was hoisted up but is still
    counted among them).

    Brief #3 added the Action Required tab which allocates 2 states per
    card slot (item_id_state + action_state). Brief #3.1 added the My
    Cases card pool — one ``case_id_state`` per slot plus a single
    ``expanded_case_id_state`` for the whole tab. Back out both tab
    contributions so this test keeps watching Intake drift only."""
    from app.surgeon_app import (
        _MAX_VISIBLE_ACTION_CARDS, _MAX_VISIBLE_MY_CASES_SLOTS,
    )

    blocks = build_surgeon_app()
    total_states = sum(
        1 for c in blocks.blocks.values() if isinstance(c, gr.State)
    )
    # AR tab: 2 states per pre-allocated slot.
    ar_states = _MAX_VISIBLE_ACTION_CARDS * 2
    # My Cases tab: 1 case_id state per slot + 1 expanded_case_id state.
    my_cases_states = _MAX_VISIBLE_MY_CASES_SLOTS + 1
    intake_states = total_states - ar_states - my_cases_states
    assert intake_states == 13


def test_success_banner_starts_empty():
    """Banner above Section 1 — empty until a submit succeeds."""
    blocks = build_surgeon_app()
    markdowns = [
        c for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    # The success banner is one of the empty-valued markdowns; can't pin it
    # uniquely by structure, but presence of at least one empty-string
    # markdown is the signal.
    empty_count = sum(1 for m in markdowns if m.value == "")
    assert empty_count >= 1


# ----- Banner formatter -----


def test_format_success_banner_includes_ucd_fil_id():
    out = _format_success_banner("UCD-FIL-005")
    assert "UCD-FIL-005" in out


def test_format_success_banner_includes_processing_hint():
    out = _format_success_banner("UCD-FIL-005")
    assert "10 minutes" in out or "minutes" in out


def test_format_success_banner_uses_checkmark():
    """Visual affordance — banner is celebratory after a successful submit."""
    out = _format_success_banner("UCD-FIL-005")
    assert out.startswith("✓") or "✓" in out


def test_clear_form_banner_constant_is_set():
    assert _CLEAR_FORM_BANNER != ""
    assert "cleared" in _CLEAR_FORM_BANNER.lower()
