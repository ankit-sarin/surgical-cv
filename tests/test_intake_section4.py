"""Tests for the Intake tab's Section 4 (notes with soft PHI regex warning).

Pure-Python coverage: the ``scan_for_phi`` regex categories (mrn / ssn /
date), the warning + counter Markdown formatters, ``_normalize_notes``
trim semantics, and Blocks construction shape. The blur-driven wirings
themselves require a running Gradio runtime to fire — covered by the
uvicorn smoke."""

from __future__ import annotations

import gradio as gr
import pytest

from app.phi import scan_for_phi
from app.surgeon_app import (
    _NOTES_HARD_LIMIT,
    _NOTES_PLACEHOLDER,
    _NOTES_SOFT_LIMIT,
    _PHI_CATEGORY_LABELS,
    _format_notes_counter,
    _format_phi_warning,
    _normalize_notes,
    build_surgeon_app,
)


# ----- scan_for_phi: empty / clean inputs -----


def test_scan_for_phi_empty_string():
    assert scan_for_phi("") == {}


def test_scan_for_phi_none():
    assert scan_for_phi(None) == {}


def test_scan_for_phi_clean_text():
    assert scan_for_phi("The patient was admitted and recovered well.") == {}


def test_scan_for_phi_year_only_stays_clean():
    """Year-only (4 digits) must not match the MRN pattern (lower bound = 7)."""
    assert scan_for_phi("Case from 2025") == {}


def test_scan_for_phi_short_numbers_stay_clean():
    """Numbers under 7 digits (room numbers, ages, etc.) must not match."""
    assert scan_for_phi("Room 4, OR 12, age 67, 6-digit 123456") == {}


# ----- scan_for_phi: MRN (7-10 unbroken digits) -----


def test_scan_for_phi_mrn_eight_digits():
    assert scan_for_phi("Patient MRN 12345678 returned.") == {"mrn": 1}


def test_scan_for_phi_mrn_seven_digits_lower_bound():
    assert scan_for_phi("ID 1234567") == {"mrn": 1}


def test_scan_for_phi_mrn_ten_digits_upper_bound():
    assert scan_for_phi("ID 1234567890") == {"mrn": 1}


def test_scan_for_phi_eleven_digits_not_matched():
    """11 digits exceeds the MRN upper bound — must not match."""
    assert scan_for_phi("ID 12345678901") == {}


def test_scan_for_phi_multiple_mrns():
    assert scan_for_phi("First 12345678 second 87654321") == {"mrn": 2}


def test_scan_for_phi_phone_number_treated_as_mrn():
    """Phone-number / MRN ambiguity is intentional v1 behavior — a naked
    10-digit run gets flagged as a long-number even if it's actually a
    phone number. The non-blocking warning is the right surface for it."""
    assert scan_for_phi("Call 5551234567") == {"mrn": 1}


# ----- scan_for_phi: SSN (XXX-XX-XXXX with separators) -----


def test_scan_for_phi_ssn_dashes():
    assert scan_for_phi("SSN 123-45-6789") == {"ssn": 1}


def test_scan_for_phi_ssn_spaces():
    assert scan_for_phi("SSN 123 45 6789") == {"ssn": 1}


def test_scan_for_phi_unseparated_nine_digits_is_mrn_not_ssn():
    """Without separators, a 9-digit run falls through to the MRN bucket
    (since 9 is in the 7-10 range). Documents the SSN/MRN ambiguity."""
    assert scan_for_phi("Number 123456789") == {"mrn": 1}


# ----- scan_for_phi: dates -----


def test_scan_for_phi_date_slashes():
    assert scan_for_phi("Surgery on 03/14/2025") == {"date": 1}


def test_scan_for_phi_date_single_digit_month_day():
    assert scan_for_phi("Date 3/14/2025") == {"date": 1}


def test_scan_for_phi_date_dashes():
    assert scan_for_phi("Date 03-14-2025") == {"date": 1}


def test_scan_for_phi_date_dots():
    assert scan_for_phi("Date 03.14.2025") == {"date": 1}


def test_scan_for_phi_multiple_dates():
    assert scan_for_phi("Started 03/14/2025 finished 03/15/2025") == {
        "date": 2,
    }


# ----- scan_for_phi: combined inputs -----


def test_scan_for_phi_spec_example_mrn_plus_date():
    """The spec example: 'Patient MRN 12345678 returned 03/14/2025'."""
    out = scan_for_phi("Patient MRN 12345678 returned 03/14/2025")
    assert out == {"mrn": 1, "date": 1}


def test_scan_for_phi_all_three_categories():
    out = scan_for_phi("MRN 12345678 SSN 123-45-6789 date 03/14/2025")
    assert out == {"mrn": 1, "ssn": 1, "date": 1}


# ----- _format_phi_warning -----


def test_format_phi_warning_empty():
    assert _format_phi_warning("") == ""


def test_format_phi_warning_none():
    assert _format_phi_warning(None) == ""


def test_format_phi_warning_clean_text():
    assert _format_phi_warning("Routine recovery.") == ""


def test_format_phi_warning_mrn_uses_long_numbers_label():
    out = _format_phi_warning("MRN 12345678")
    assert "long numbers (1)" in out
    assert "Possible PHI detected" in out


def test_format_phi_warning_date_uses_dates_label():
    out = _format_phi_warning("Date 03/14/2025")
    assert "dates (1)" in out


def test_format_phi_warning_ssn_uses_ssn_like_label():
    out = _format_phi_warning("SSN 123-45-6789")
    assert "SSN-like format (1)" in out


def test_format_phi_warning_combined_matches_spec_phrase():
    """Spec example output shape: 'long numbers (n), dates (m)'."""
    out = _format_phi_warning("Patient MRN 12345678 returned 03/14/2025")
    assert "long numbers (1)" in out
    assert "dates (1)" in out
    # mrn comes before date per the category insertion order in
    # _PHI_CATEGORY_LABELS — guard against accidental reordering.
    assert out.index("long numbers") < out.index("dates")


def test_format_phi_warning_mentions_confirm_at_submission():
    out = _format_phi_warning("MRN 12345678")
    assert "confirm at submission" in out


def test_format_phi_warning_never_leaks_matched_text():
    """Privacy contract: the warning surface must never include the
    actual matched digits / patterns — only category counts."""
    out = _format_phi_warning(
        "MRN 12345678 SSN 123-45-6789 date 03/14/2025"
    )
    assert "12345678" not in out
    assert "123-45-6789" not in out
    assert "03/14/2025" not in out


# ----- _format_notes_counter -----


def test_format_notes_counter_zero_is_neutral():
    out = _format_notes_counter(0)
    assert "⚠" not in out
    assert "0" in out


def test_format_notes_counter_under_soft_limit_is_neutral():
    out = _format_notes_counter(_NOTES_SOFT_LIMIT - 1)
    assert "⚠" not in out


def test_format_notes_counter_at_soft_limit_is_amber():
    out = _format_notes_counter(_NOTES_SOFT_LIMIT)
    assert "⚠" in out


def test_format_notes_counter_above_soft_limit_is_amber():
    out = _format_notes_counter(_NOTES_HARD_LIMIT - 1)
    assert "⚠" in out


def test_format_notes_counter_always_shows_hard_limit():
    """The hard cap is always visible so the user knows the ceiling."""
    out = _format_notes_counter(100)
    assert str(_NOTES_HARD_LIMIT) in out


# ----- _normalize_notes -----


def test_normalize_notes_trims_whitespace():
    assert _normalize_notes("  some text  ") == "some text"


def test_normalize_notes_preserves_internal_whitespace():
    assert _normalize_notes("line one  line two") == "line one  line two"


def test_normalize_notes_empty_returns_none():
    assert _normalize_notes("") is None


def test_normalize_notes_whitespace_only_returns_none():
    assert _normalize_notes("   \n\t  ") is None


def test_normalize_notes_none_returns_none():
    assert _normalize_notes(None) is None


# ----- Blocks introspection -----


def test_intake_tab_carries_section4_header():
    blocks = build_surgeon_app()
    markdown_values = [
        c.value for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    assert any(v and "Section 4" in str(v) for v in markdown_values)


def test_intake_tab_carries_thirteen_state_components():
    """Section 1 (3: segments, selected, show_more) + picklists_state (1) +
    Section 2 (4: procedure_primary, procedure_additional, approach,
    conversion_target) + Section 3 (3: case_year, or_room, indication) +
    Section 4 (2: notes, notes_phi_warnings) = 13 total."""
    blocks = build_surgeon_app()
    state_count = sum(
        1 for c in blocks.blocks.values() if isinstance(c, gr.State)
    )
    assert state_count >= 13


def test_intake_tab_carries_notes_textbox():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    case_notes = [t for t in textboxes if t.label == "Case notes"]
    assert len(case_notes) == 1


def test_notes_textbox_has_six_lines():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    notes_tb = [t for t in textboxes if t.label == "Case notes"][0]
    assert notes_tb.lines == 6


def test_notes_textbox_max_length_is_hard_limit():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    notes_tb = [t for t in textboxes if t.label == "Case notes"][0]
    assert notes_tb.max_length == _NOTES_HARD_LIMIT
    assert _NOTES_HARD_LIMIT == 1000


def test_notes_textbox_placeholder_mentions_phi():
    blocks = build_surgeon_app()
    textboxes = [c for c in blocks.blocks.values() if isinstance(c, gr.Textbox)]
    notes_tb = [t for t in textboxes if t.label == "Case notes"][0]
    assert notes_tb.placeholder == _NOTES_PLACEHOLDER
    assert "PHI" in _NOTES_PLACEHOLDER


# ----- Constants -----


def test_phi_category_labels_cover_all_three_categories():
    assert set(_PHI_CATEGORY_LABELS.keys()) == {"mrn", "ssn", "date"}


def test_phi_category_labels_humanize_mrn_to_long_numbers():
    """The user-facing label intentionally hides the "MRN" jargon — the
    pattern catches phone numbers too, so "long numbers" is the more
    honest category name."""
    assert _PHI_CATEGORY_LABELS["mrn"] == "long numbers"


def test_soft_limit_below_hard_limit():
    assert _NOTES_SOFT_LIMIT < _NOTES_HARD_LIMIT


def test_soft_and_hard_limits_match_spec():
    assert _NOTES_SOFT_LIMIT == 500
    assert _NOTES_HARD_LIMIT == 1000
