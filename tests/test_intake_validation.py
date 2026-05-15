"""Tests for the Section 5 validation accumulator and orchestrator in
``app/intake/submit.py``. Pure-Python — the gradio handler integration is
exercised separately in test_intake_section5.py."""

from __future__ import annotations

import pytest

from app.intake.submit import (
    SubmitOutcome,
    ValidationContext,
    build_partial_row,
    format_validation_errors,
    handle_submit_request,
    validate_submission,
)
from app.repos.cases import InMemoryCaseRepository, SubmitError
from app.repos.picklists import PicklistValue


def _picklists() -> dict[str, list[PicklistValue]]:
    return {
        "procedure": [
            PicklistValue("Sigmoidectomy", "Sigmoidectomy", 10),
            PicklistValue("Right hemicolectomy", "Right hemicolectomy", 20),
            PicklistValue("Low anterior resection", "Low anterior resection", 30),
            PicklistValue("TAMIS", "TAMIS", 40),
        ],
        "approach": [
            PicklistValue("Open", "Open", 10),
            PicklistValue("Laparoscopic", "Laparoscopic", 20),
            PicklistValue("Robotic", "Robotic", 30),
            PicklistValue("Hybrid", "Hybrid", 40),
        ],
        "case_year": [
            PicklistValue("2026", "2026", 10),
            PicklistValue("2025", "2025", 20),
        ],
        "indication": [
            PicklistValue("Colorectal cancer", "Colorectal cancer", 10),
            PicklistValue("Diverticulitis", "Diverticulitis", 20),
        ],
    }


def _valid_ctx(**overrides) -> ValidationContext:
    base = dict(
        segments_selected=["capt0_20260515-080000.mp4"],
        procedure_primary="Sigmoidectomy",
        procedure_additional=[],
        approach="Robotic",
        conversion_target=None,
        case_year="2026",
        or_room="OR 4",
        indication="Colorectal cancer",
    )
    base.update(overrides)
    return ValidationContext(**base)


# ----- validate_submission: happy path -----


def test_validate_clean_returns_empty():
    assert validate_submission(_valid_ctx(), _picklists()) == []


# ----- validate_submission: segments -----


def test_validate_empty_segments_errors():
    errors = validate_submission(_valid_ctx(segments_selected=[]), _picklists())
    assert any("segment" in e.lower() for e in errors)


# ----- validate_submission: procedure_primary -----


def test_validate_none_primary_errors():
    errors = validate_submission(
        _valid_ctx(procedure_primary=None), _picklists()
    )
    assert any("Primary procedure required" in e for e in errors)


def test_validate_unknown_primary_errors():
    errors = validate_submission(
        _valid_ctx(procedure_primary="MadeUp"), _picklists()
    )
    assert any("not in the vocabulary" in e for e in errors)


# ----- validate_submission: procedure_additional -----


def test_validate_additional_in_vocab_clean():
    assert validate_submission(
        _valid_ctx(procedure_additional=["TAMIS"]), _picklists()
    ) == []


def test_validate_additional_not_in_vocab_errors():
    errors = validate_submission(
        _valid_ctx(procedure_additional=["MadeUp"]), _picklists()
    )
    assert any("MadeUp" in e for e in errors)


def test_validate_additional_duplicates_primary_errors():
    errors = validate_submission(
        _valid_ctx(
            procedure_primary="Sigmoidectomy",
            procedure_additional=["Sigmoidectomy"],
        ),
        _picklists(),
    )
    assert any("primary" in e.lower() for e in errors)


def test_validate_additional_internal_dup_errors():
    errors = validate_submission(
        _valid_ctx(procedure_additional=["TAMIS", "TAMIS"]),
        _picklists(),
    )
    assert any("duplicate" in e.lower() for e in errors)


# ----- validate_submission: approach -----


def test_validate_none_approach_errors():
    errors = validate_submission(_valid_ctx(approach=None), _picklists())
    assert any("Approach required" in e for e in errors)


def test_validate_unknown_approach_errors():
    errors = validate_submission(
        _valid_ctx(approach="Spaceship"), _picklists()
    )
    assert any("Spaceship" in e for e in errors)


# ----- validate_submission: conversion_target -----


def test_validate_none_conversion_target_clean():
    """None = "not a conversion case" — valid."""
    assert validate_submission(
        _valid_ctx(conversion_target=None), _picklists()
    ) == []


def test_validate_empty_string_conversion_target_errors():
    """Spec G's "checked but no target picked" sentinel — must fail with
    a select-target message."""
    errors = validate_submission(
        _valid_ctx(conversion_target=""), _picklists()
    )
    assert any("select a conversion target" in e.lower() for e in errors)


def test_validate_unknown_conversion_target_errors():
    errors = validate_submission(
        _valid_ctx(conversion_target="MadeUp"), _picklists()
    )
    assert any("MadeUp" in e for e in errors)


def test_validate_conversion_equal_to_approach_errors():
    errors = validate_submission(
        _valid_ctx(approach="Robotic", conversion_target="Robotic"),
        _picklists(),
    )
    assert any(
        "cannot equal" in e.lower() or "equal" in e.lower() for e in errors
    )


def test_validate_conversion_different_from_approach_clean():
    assert validate_submission(
        _valid_ctx(approach="Robotic", conversion_target="Open"),
        _picklists(),
    ) == []


# ----- validate_submission: case_year -----


def test_validate_none_case_year_errors():
    errors = validate_submission(_valid_ctx(case_year=None), _picklists())
    assert any("Case year required" in e for e in errors)


def test_validate_unknown_case_year_errors():
    errors = validate_submission(_valid_ctx(case_year="1999"), _picklists())
    assert any("1999" in e for e in errors)


# ----- validate_submission: or_room -----


def test_validate_none_or_room_errors():
    errors = validate_submission(_valid_ctx(or_room=None), _picklists())
    assert any("OR room required" in e for e in errors)


# ----- validate_submission: indication -----


def test_validate_none_indication_errors():
    errors = validate_submission(_valid_ctx(indication=None), _picklists())
    assert any("Indication required" in e for e in errors)


def test_validate_unknown_indication_errors():
    errors = validate_submission(
        _valid_ctx(indication="MadeUp"), _picklists()
    )
    assert any("MadeUp" in e for e in errors)


# ----- validate_submission: multi-error accumulation -----


def test_validate_multi_error_accumulates_all():
    """Validator walks every section, doesn't bail on first miss — surgeon
    sees the full punch list."""
    errors = validate_submission(
        ValidationContext(
            segments_selected=[],
            procedure_primary=None,
            procedure_additional=[],
            approach=None,
            conversion_target=None,
            case_year=None,
            or_room=None,
            indication=None,
        ),
        _picklists(),
    )
    # At least 6 errors (one per missing required field).
    assert len(errors) >= 6


# ----- format_validation_errors -----


def test_format_empty_returns_empty():
    assert format_validation_errors([]) == ""


def test_format_includes_each_error_as_bullet():
    out = format_validation_errors(["Error A", "Error B"])
    assert "Error A" in out
    assert "Error B" in out
    assert "- " in out


def test_format_has_header():
    out = format_validation_errors(["x"])
    assert "fix" in out.lower() or "please" in out.lower()


# ----- build_partial_row -----


def test_build_partial_row_normalizes_conversion_none_to_empty():
    row = build_partial_row(
        "sarin", _valid_ctx(conversion_target=None), notes=None
    )
    assert row["conversion_target"] == ""


def test_build_partial_row_preserves_conversion_value():
    row = build_partial_row(
        "sarin",
        _valid_ctx(approach="Robotic", conversion_target="Open"),
        notes=None,
    )
    assert row["conversion_target"] == "Open"


def test_build_partial_row_normalizes_notes_none_to_empty():
    row = build_partial_row("sarin", _valid_ctx(), notes=None)
    assert row["notes"] == ""


def test_build_partial_row_includes_surgeon():
    row = build_partial_row("miller", _valid_ctx(), notes=None)
    assert row["surgeon"] == "miller"


# ----- handle_submit_request orchestration -----


def test_handle_validation_failure_returns_validation_error():
    repo = InMemoryCaseRepository()
    outcome = handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(procedure_primary=None),
        notes=None,
        notes_phi_warnings={},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=repo.submit_case,
        phi_already_confirmed=False,
    )
    assert outcome.kind == "validation_error"
    assert outcome.error_block != ""


def test_handle_phi_present_returns_phi_confirm():
    repo = InMemoryCaseRepository()
    outcome = handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(),
        notes="MRN 12345678",
        notes_phi_warnings={"mrn": 1},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=repo.submit_case,
        phi_already_confirmed=False,
    )
    assert outcome.kind == "phi_confirm"
    # Repo must not have been touched.
    assert repo.list_owned_by("sarin") == []


def test_handle_phi_already_confirmed_bypasses_gate():
    repo = InMemoryCaseRepository()
    outcome = handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(),
        notes="MRN 12345678",
        notes_phi_warnings={"mrn": 1},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=repo.submit_case,
        phi_already_confirmed=True,
    )
    assert outcome.kind == "success"
    assert outcome.submit_result is not None


def test_handle_clean_no_phi_returns_success():
    repo = InMemoryCaseRepository()
    outcome = handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(),
        notes=None,
        notes_phi_warnings={},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=repo.submit_case,
        phi_already_confirmed=False,
    )
    assert outcome.kind == "success"
    assert outcome.submit_result.ucd_fil_id == "UCD-FIL-001"


def test_handle_infra_error_surfaces_as_infra_error_kind():
    def boom(_row, _segments):
        raise SubmitError("disk full")

    outcome = handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(),
        notes=None,
        notes_phi_warnings={},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=boom,
        phi_already_confirmed=False,
    )
    assert outcome.kind == "infra_error"
    assert "disk full" in outcome.infra_error


def test_handle_validation_failure_does_not_call_submit_fn():
    """Defense in depth: validation must short-circuit the submit_fn."""
    called = []

    def tracker(row, segments):
        called.append((row, segments))
        return None

    handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(procedure_primary=None),
        notes=None,
        notes_phi_warnings={},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=tracker,
        phi_already_confirmed=False,
    )
    assert called == []


def test_handle_phi_warnings_with_empty_dict_skips_gate():
    """An empty PHI dict (no patterns matched) must NOT trigger the gate."""
    repo = InMemoryCaseRepository()
    outcome = handle_submit_request(
        surgeon="sarin",
        ctx=_valid_ctx(),
        notes="clean notes",
        notes_phi_warnings={},
        picklists=_picklists(),
        segment_filenames=["a.mp4"],
        submit_fn=repo.submit_case,
        phi_already_confirmed=False,
    )
    assert outcome.kind == "success"
