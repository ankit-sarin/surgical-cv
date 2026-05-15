"""Tests for ``app/phi.py`` — the intake-time scanner. The legacy MRN/SSN/
date pattern coverage lives in ``tests/test_intake_section4.py`` (Section 4
UI fixture) and stays there. This file covers the F-005 extensions: name,
phone, and address pattern detection through the same ``scan_for_phi``
interface."""

from __future__ import annotations

from app.phi import scan_for_phi


# ----- name pattern -----


def test_scan_name_dr_period_prefix():
    assert scan_for_phi("Reviewed by Dr. Smith") == {"name": 1}


def test_scan_name_patient_colon_prefix_multi_token():
    """A first+last name after Patient: counts as one match (the regex
    consumes consecutive title-case tokens as a single span)."""
    assert scan_for_phi("Patient: John Doe arrived") == {"name": 1}


def test_scan_name_lowercase_word_does_not_trigger():
    """Common nouns starting with a capital ('Patient was admitted') must
    not trigger — the pattern requires a deliberate clinical prefix."""
    assert scan_for_phi("The patient was admitted and recovered well.") == {}


def test_scan_name_all_caps_token_after_prefix_does_not_trigger():
    """'Patient MRN 12345678' — MRN is all-caps, not title-case, so the
    NAME pattern doesn't fire (only the MRN pattern does). Guards a known
    fixture in test_intake_section4.py from regressing."""
    out = scan_for_phi("Patient MRN 12345678 returned")
    assert "name" not in out
    assert out.get("mrn") == 1


# ----- phone pattern -----


def test_scan_phone_dashed_separator():
    assert scan_for_phi("Call 916-555-1234").get("phone") == 1


def test_scan_phone_paren_area_code():
    assert scan_for_phi("Call (916) 555-1234").get("phone") == 1


def test_scan_phone_dotted_separator():
    assert scan_for_phi("Call 916.555.1234").get("phone") == 1


def test_scan_naked_ten_digit_remains_mrn_not_phone():
    """No separator → falls through to MRN per the v1 ambiguity policy
    (see app/phi.py docstring)."""
    out = scan_for_phi("Call 5551234567")
    assert out == {"mrn": 1}


# ----- address pattern -----


def test_scan_address_simple():
    assert scan_for_phi("Lives at 123 Main St").get("address") == 1


def test_scan_address_multi_word_street():
    assert scan_for_phi("Lives at 4567 Oak Park Ave").get("address") == 1


def test_scan_address_dr_suffix_distinguished_from_doctor_prefix():
    """'Acacia Dr.' (street) requires a leading number — without one the
    address pattern doesn't fire, so 'Dr. Smith' (doctor prefix without a
    leading number) doesn't get mis-categorized as an address."""
    out = scan_for_phi("Reviewed by Dr. Smith")
    assert "address" not in out


# ----- combined / regression -----


def test_scan_combined_six_categories():
    """Worst-case sentence with every category. Counts may vary per the
    pattern ordering; we just assert each category fires at least once."""
    text = (
        "Patient: John Smith, MRN 12345678, SSN 123-45-6789, "
        "phone (916) 555-1234, lives at 123 Main St, seen on 03/14/2025"
    )
    out = scan_for_phi(text)
    for category in ("name", "mrn", "ssn", "phone", "address", "date"):
        assert out.get(category, 0) >= 1, f"missed {category} in {out}"
