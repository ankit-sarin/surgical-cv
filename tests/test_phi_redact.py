"""Tests for ``pipeline/phi_redact.py`` — field-aware redact_field +
content-aware scrub_text. The patterns themselves are exercised at the
``app/phi.py`` level via ``tests/test_phi.py``; here we verify the
redaction-surface behavior."""

from __future__ import annotations

from pipeline.phi_redact import REDACTED_FIELDS, redact_field, scrub_text


# ----- redact_field: field-aware -----


def test_redact_field_known_field_redacts_with_length():
    out = redact_field("notes", "airway issues during induction")
    assert out == "<redacted, length=30>"


def test_redact_field_known_field_empty_returns_empty_marker():
    """Preserves the prior --show UX: empty notes still rendered as
    (empty), not as <redacted, length=0>."""
    assert redact_field("notes", "") == "(empty)"
    assert redact_field("notes", None) == "(empty)"  # type: ignore[arg-type]


def test_redact_field_unknown_field_passes_through():
    """Non-sensitive fields are not redacted — the column whitelist is in
    REDACTED_FIELDS."""
    assert redact_field("procedure_primary", "Sigmoidectomy") == "Sigmoidectomy"
    assert redact_field("approach", "Robotic") == "Robotic"
    assert redact_field("case_year", "2026") == "2026"


def test_redact_field_unknown_field_none_returns_empty_string():
    """None on a non-sensitive field collapses to empty string (caller may
    still want to render '(empty)' explicitly)."""
    assert redact_field("approach", None) == ""  # type: ignore[arg-type]


def test_redacted_fields_includes_notes():
    """Sanity: notes is the canonical sensitive field. If the set ever
    shrinks, the metadata --show contract changes too."""
    assert "notes" in REDACTED_FIELDS


# ----- scrub_text: content-aware -----


def test_scrub_text_empty_input_returns_empty():
    assert scrub_text("") == ""
    assert scrub_text(None) == ""  # type: ignore[arg-type]


def test_scrub_text_no_phi_passes_through():
    """Clean text survives the scrub round-trip unchanged — no false
    placeholders inserted into normal operator messages."""
    msg = "FFmpeg failed: invalid codec parameter"
    assert scrub_text(msg) == msg


def test_scrub_text_replaces_mrn():
    out = scrub_text("Patient MRN 12345678 returned")
    assert "12345678" not in out
    assert "<MRN>" in out


def test_scrub_text_replaces_ssn():
    out = scrub_text("SSN 123-45-6789 on file")
    assert "123-45-6789" not in out
    assert "<SSN>" in out


def test_scrub_text_replaces_date():
    out = scrub_text("Surgery on 03/14/2025")
    assert "03/14/2025" not in out
    assert "<DATE>" in out


def test_scrub_text_replaces_name_with_dr_prefix():
    out = scrub_text("Reviewed by Dr. Smith and Dr. John Doe")
    assert "Dr. Smith" not in out
    assert "Dr. John Doe" not in out
    assert "<NAME>" in out


def test_scrub_text_replaces_phone_with_separators():
    """Separator-formatted phones get their own placeholder. Naked 10-digit
    runs would be caught by MRN, not PHONE — that ambiguity is intentional
    (see app/phi.py docstring)."""
    out = scrub_text("Call (916) 555-1234 or 916-555-1234")
    assert "(916) 555-1234" not in out
    assert "916-555-1234" not in out
    assert "<PHONE>" in out


def test_scrub_text_replaces_address():
    out = scrub_text("Lives at 123 Main St near campus")
    assert "123 Main St" not in out
    assert "<ADDRESS>" in out


def test_scrub_text_combined_pii_all_redacted():
    """Worst-case sentence: every category present. Verifies independent
    redaction (one pattern's substitution doesn't break another). The
    Patient: prefix (with colon) is the canonical form that triggers the
    NAME pattern — see app/phi.py docstring."""
    msg = (
        "Patient: John Smith, MRN 12345678, lives at 123 Main St, "
        "phone (916) 555-1234, surgery on 03/14/2025, SSN 123-45-6789"
    )
    out = scrub_text(msg)
    # No raw PHI survives (sample a few representative substrings).
    assert "12345678" not in out
    assert "123-45-6789" not in out
    assert "03/14/2025" not in out
    assert "(916) 555-1234" not in out
    assert "John Smith" not in out
    # Structural context (the wrapping prose) does survive.
    assert "lives at" in out
    assert "surgery on" in out
