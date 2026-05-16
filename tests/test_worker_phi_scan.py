"""Tests for ``app/worker/phi_scan.py::redact_case_notes`` (Brief #3.5a).

Covers:

1. Clean notes → manifest untouched, return ``{}``.
2. PHI present → notes scrubbed in place, return matches the
   ``scan_for_phi`` result.
3. Idempotent re-scan → second call returns ``{}`` and the manifest
   is unchanged on the second call.
4. Missing manifest row → returns ``{}``, no exception, manifest
   untouched.
5. Atomic write — partial-failure mid-commit leaves the manifest
   intact (no partial ``.tmp`` artifacts left behind).

The PHI fixture uses sentinel strings so the assertions can't
false-positive on common substrings:
  name = "Zxqv Wmrp"
  mrn  = "99887766"
  date = "1/14/1962"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    CaseManifestRow,
)

from app.phi import scan_for_phi
from app.worker.phi_scan import redact_case_notes


# ----- fixtures -----


@pytest.fixture
def paths(tmp_path) -> NasPaths:
    or_raw = tmp_path / "or-raw"
    or_raw.mkdir()
    return NasPaths(
        root=tmp_path,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )


def _seed_manifest(paths: NasPaths, case_id: str, notes: str):
    """Append one CaseManifestRow with the given notes."""
    table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    with table.transaction() as tx:
        tx.append(CaseManifestRow(
            ucd_fil_id=case_id,
            surgeon="sarin",
            case_year="2026",
            or_room="OR 4",
            procedure_primary="Sigmoidectomy",
            procedure_additional=[],
            approach="Robotic",
            conversion_target="",
            indication="Colorectal cancer",
            notes=notes,
        ))


def _read_notes(paths: NasPaths, case_id: str) -> str:
    table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    for row in table.snapshot():
        if row.ucd_fil_id == case_id:
            return row.notes
    raise AssertionError(f"case {case_id} not in manifest")


# Sentinel PHI fixture. Distinct, hard-to-confuse values so the
# "details must not contain the raw value" assertions can't
# false-positive.
_PHI_NOTES = (
    "Pt. Zxqv Wmrp MRN: 99887766, dob 1/14/1962. "
    "Sigmoid resection planned."
)
_PHI_RAW_VALUES = ("Zxqv Wmrp", "99887766", "1/14/1962")


# ----- 1. clean notes → manifest untouched -----


def test_clean_notes_returns_empty_and_leaves_manifest_unchanged(paths):
    _seed_manifest(paths, "UCD-FIL-001", "sigmoidectomy, uneventful")
    snapshot_before = paths.manifest_csv.read_bytes()

    result = redact_case_notes(paths, "UCD-FIL-001")

    assert result == {}
    assert paths.manifest_csv.read_bytes() == snapshot_before
    # And the row's notes are still the original string verbatim.
    assert _read_notes(paths, "UCD-FIL-001") == "sigmoidectomy, uneventful"


def test_empty_notes_returns_empty_and_no_write(paths):
    """Empty / None notes path through the same fast exit."""
    _seed_manifest(paths, "UCD-FIL-001", "")
    snapshot_before = paths.manifest_csv.read_bytes()

    result = redact_case_notes(paths, "UCD-FIL-001")

    assert result == {}
    assert paths.manifest_csv.read_bytes() == snapshot_before


# ----- 2. PHI present → scrubbed in place -----


def test_phi_present_scrubs_notes_in_place(paths):
    _seed_manifest(paths, "UCD-FIL-001", _PHI_NOTES)
    expected_scan = scan_for_phi(_PHI_NOTES)
    # Sanity check on the fixture itself — if scan_for_phi changes
    # vocabulary, this assertion catches it before the redact test.
    assert "name" in expected_scan
    assert "mrn" in expected_scan
    assert "date" in expected_scan

    result = redact_case_notes(paths, "UCD-FIL-001")

    assert result == expected_scan

    scrubbed = _read_notes(paths, "UCD-FIL-001")
    # Placeholders are in the scrubbed text.
    assert "<NAME>" in scrubbed
    assert "<MRN>" in scrubbed
    assert "<DATE>" in scrubbed
    # And NONE of the raw PHI values survive — the load-bearing
    # property of this brief.
    for raw_value in _PHI_RAW_VALUES:
        assert raw_value not in scrubbed, (
            f"raw PHI value {raw_value!r} leaked into the scrubbed "
            f"notes column: {scrubbed!r}"
        )


# ----- 3. idempotent re-scan -----


def test_idempotent_rescan_returns_empty_and_no_second_write(paths):
    """Running redact_case_notes twice on the same case yields PHI
    found on the first call and a clean no-op on the second. The
    manifest must be byte-stable across the second call."""
    _seed_manifest(paths, "UCD-FIL-001", _PHI_NOTES)

    first_result = redact_case_notes(paths, "UCD-FIL-001")
    assert first_result  # non-empty — PHI was present
    snapshot_after_first = paths.manifest_csv.read_bytes()

    second_result = redact_case_notes(paths, "UCD-FIL-001")
    assert second_result == {}
    assert paths.manifest_csv.read_bytes() == snapshot_after_first


# ----- 4. missing manifest row → safe exit -----


def test_missing_case_id_returns_empty_no_exception(paths):
    """Manifest file exists but doesn't contain the case_id — common
    if the worker is racing a parallel admin operation, or if the
    marker references a non-existent ucd_fil_id. Must not raise; must
    not write."""
    _seed_manifest(paths, "UCD-FIL-001", "something else")
    snapshot_before = paths.manifest_csv.read_bytes()

    result = redact_case_notes(paths, "UCD-FIL-999")

    assert result == {}
    assert paths.manifest_csv.read_bytes() == snapshot_before


# ----- 5. atomic write — partial-failure leaves manifest intact -----


def test_commit_failure_leaves_manifest_valid(paths, monkeypatch):
    """Monkeypatch ``CsvTable._commit`` to raise mid-write. The
    original manifest file must remain byte-stable and parseable —
    the pipeline.atomic_write primitive guarantees no partial
    artifacts (tempfile cleanup on exception)."""
    _seed_manifest(paths, "UCD-FIL-001", _PHI_NOTES)
    snapshot_before = paths.manifest_csv.read_bytes()

    boom = RuntimeError("synthetic commit failure")

    def _explode(self, tx):
        raise boom

    monkeypatch.setattr(CsvTable, "_commit", _explode)

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        redact_case_notes(paths, "UCD-FIL-001")

    # Original manifest unchanged byte-for-byte.
    assert paths.manifest_csv.read_bytes() == snapshot_before
    # And no stray .tmp artifacts left over alongside the manifest.
    stray_tmps = list(paths.manifest_csv.parent.glob(
        paths.manifest_csv.name + ".*.tmp"
    ))
    assert stray_tmps == [], f"leftover tempfiles: {stray_tmps}"
