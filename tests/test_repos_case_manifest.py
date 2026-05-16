"""Tests for ``app/repos/case_manifest.py`` — CsvCaseManifestRepository
against a tmpdir CSV fixture, plus InMemoryCaseManifestRepository's
pure-Python behavior. Mirrors ``test_pipeline_state_repos.py``.

Coverage targets (Brief #3.1 §5 step 2):
  - Row parsing — every column round-trips faithfully
  - procedure_additional JSON parsing — empty / single / multi / malformed
  - missing case_id → None (not exception)
  - empty file → None
  - missing file → None (defensive — the manifest may not exist in tests)
  - malformed row (non-list JSON in procedure_additional) → tolerated
  - env-var path resolution
  - stateless re-read (mutate the file → second call sees the new state)
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.repos.case_manifest import (
    CaseManifestRow,
    CsvCaseManifestRepository,
    InMemoryCaseManifestRepository,
    manifest_path,
)
from pipeline.schemas import CASE_MANIFEST_COLUMNS


_HEADER = ",".join(CASE_MANIFEST_COLUMNS)


def _row(
    ucd_fil_id: str = "UCD-FIL-001",
    surgeon: str = "sarin",
    case_year: str = "2026",
    or_room: str = "OR 4",
    procedure_primary: str = "Low anterior resection",
    procedure_additional: str = "",
    approach: str = "Robotic",
    conversion_target: str = "",
    indication: str = "Colorectal cancer",
    notes: str = "",
) -> dict[str, str]:
    """Return the dict shape; ``_write_manifest`` serializes via the csv
    module so JSON arrays / quote-embedding cells round-trip cleanly."""
    return {
        "ucd_fil_id": ucd_fil_id,
        "surgeon": surgeon,
        "case_year": case_year,
        "or_room": or_room,
        "procedure_primary": procedure_primary,
        "procedure_additional": procedure_additional,
        "approach": approach,
        "conversion_target": conversion_target,
        "indication": indication,
        "notes": notes,
    }


def _write_manifest(target: Path, rows: list[dict[str, str]]) -> Path:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CASE_MANIFEST_COLUMNS))
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    target.write_text(buf.getvalue())
    return target


# ----- manifest_path() env override -----


def test_manifest_path_honors_env(monkeypatch, tmp_path):
    custom = tmp_path / "custom_manifest.csv"
    monkeypatch.setenv("CASE_MANIFEST_PATH", str(custom))
    assert manifest_path() == custom


def test_manifest_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("CASE_MANIFEST_PATH", raising=False)
    p = manifest_path()
    assert p.name == "case_manifest.csv"
    assert "/mnt/nas/or-raw" in str(p)


# ----- CsvCaseManifestRepository — happy paths -----


def test_csv_for_case_id_returns_typed_row(tmp_path):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001")],
    )
    repo = CsvCaseManifestRepository(target)
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert isinstance(row, CaseManifestRow)
    assert row.ucd_fil_id == "UCD-FIL-001"
    assert row.surgeon == "sarin"
    assert row.case_year == "2026"
    assert row.or_room == "OR 4"
    assert row.procedure_primary == "Low anterior resection"
    assert row.procedure_additional == ()
    assert row.approach == "Robotic"
    assert row.conversion_target == ""
    assert row.indication == "Colorectal cancer"
    assert row.notes == ""


def test_csv_for_case_id_unknown_returns_none(tmp_path):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001")],
    )
    repo = CsvCaseManifestRepository(target)
    assert repo.for_case_id("UCD-FIL-999") is None


def test_csv_for_case_id_empty_file_returns_none(tmp_path):
    target = _write_manifest(tmp_path / "m.csv", [])
    repo = CsvCaseManifestRepository(target)
    assert repo.for_case_id("UCD-FIL-001") is None


def test_csv_missing_file_returns_none(tmp_path):
    repo = CsvCaseManifestRepository(tmp_path / "does-not-exist.csv")
    assert repo.for_case_id("UCD-FIL-001") is None


# ----- procedure_additional JSON parsing -----


@pytest.mark.parametrize("encoded,expected", [
    ("", ()),
    ("[]", ()),
    ('["Loop ileostomy"]', ("Loop ileostomy",)),
    ('["Loop ileostomy","Splenic flexure takedown"]',
     ("Loop ileostomy", "Splenic flexure takedown")),
])
def test_csv_procedure_additional_parses_json_array(
    tmp_path, encoded, expected
):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001", procedure_additional=encoded)],
    )
    repo = CsvCaseManifestRepository(target)
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert row.procedure_additional == expected


def test_csv_procedure_additional_malformed_json_collapses_to_empty(
    tmp_path
):
    """Defensive: a row with garbage in procedure_additional must not
    take the surgeon UI offline. The CLI metadata --edit gate is the
    write-side validator; the read path stays tolerant."""
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001", procedure_additional="not-json")],
    )
    repo = CsvCaseManifestRepository(target)
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert row.procedure_additional == ()


def test_csv_procedure_additional_non_array_json_collapses_to_empty(
    tmp_path
):
    """JSON that parses but isn't an array (e.g., a stray object)
    silently degrades to empty rather than raising."""
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001", procedure_additional='{"foo":"bar"}')],
    )
    repo = CsvCaseManifestRepository(target)
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert row.procedure_additional == ()


# ----- conversion_target / notes round-trip -----


def test_csv_conversion_target_when_case_was_converted(tmp_path):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row(
            "UCD-FIL-001",
            approach="Robotic",
            conversion_target="Open",
        )],
    )
    repo = CsvCaseManifestRepository(target)
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert row.approach == "Robotic"
    assert row.conversion_target == "Open"


def test_csv_notes_round_trip(tmp_path):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001", notes="brief case note")],
    )
    repo = CsvCaseManifestRepository(target)
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert row.notes == "brief case note"


# ----- env-resolved path -----


def test_csv_env_var_path(monkeypatch, tmp_path):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001")],
    )
    monkeypatch.setenv("CASE_MANIFEST_PATH", str(target))
    repo = CsvCaseManifestRepository()  # no explicit path → reads env
    row = repo.for_case_id("UCD-FIL-001")
    assert row is not None
    assert row.ucd_fil_id == "UCD-FIL-001"


# ----- stateless re-read -----


def test_csv_reads_fresh_each_call(tmp_path):
    target = _write_manifest(
        tmp_path / "m.csv",
        [_row("UCD-FIL-001", or_room="OR 4")],
    )
    repo = CsvCaseManifestRepository(target)
    first = repo.for_case_id("UCD-FIL-001")
    assert first is not None
    assert first.or_room == "OR 4"

    _write_manifest(
        target,
        [_row("UCD-FIL-001", or_room="ASC OR 2")],
    )
    second = repo.for_case_id("UCD-FIL-001")
    assert second is not None
    assert second.or_room == "ASC OR 2"


# ----- multi-row filtering -----


def test_csv_for_case_id_filters_among_many(tmp_path):
    """A larger manifest: ``for_case_id`` returns the right row and
    only that row, regardless of position."""
    target = _write_manifest(
        tmp_path / "m.csv",
        [
            _row("UCD-FIL-001", surgeon="sarin"),
            _row("UCD-FIL-002", surgeon="sarin"),
            _row("UCD-FIL-099", surgeon="miller", or_room="OR 1"),
            _row("UCD-FIL-100", surgeon="noren"),
        ],
    )
    repo = CsvCaseManifestRepository(target)
    miller = repo.for_case_id("UCD-FIL-099")
    assert miller is not None
    assert miller.surgeon == "miller"
    assert miller.or_room == "OR 1"


# ----- InMemoryCaseManifestRepository -----


def test_inmem_for_case_id_with_typed_rows():
    row = CaseManifestRow(
        ucd_fil_id="UCD-FIL-001",
        surgeon="sarin",
        case_year="2026",
        or_room="OR 4",
        procedure_primary="Low anterior resection",
        procedure_additional=("Loop ileostomy",),
        approach="Robotic",
        conversion_target="",
        indication="Colorectal cancer",
        notes="",
    )
    repo = InMemoryCaseManifestRepository([row])
    out = repo.for_case_id("UCD-FIL-001")
    assert out is row  # identity preserved (frozen dataclass)


def test_inmem_for_case_id_with_dict_rows():
    """Accepts on-disk-shape dicts for ergonomic fixtures — same code
    path the CsvCaseManifestRepository uses under the hood."""
    repo = InMemoryCaseManifestRepository([
        {
            "ucd_fil_id": "UCD-FIL-001",
            "surgeon": "sarin",
            "case_year": "2026",
            "or_room": "OR 4",
            "procedure_primary": "Low anterior resection",
            "procedure_additional": '["Loop ileostomy"]',
            "approach": "Robotic",
            "conversion_target": "",
            "indication": "Colorectal cancer",
            "notes": "",
        },
    ])
    out = repo.for_case_id("UCD-FIL-001")
    assert out is not None
    assert out.procedure_additional == ("Loop ileostomy",)


def test_inmem_for_case_id_unknown_returns_none():
    repo = InMemoryCaseManifestRepository([])
    assert repo.for_case_id("UCD-FIL-001") is None


def test_inmem_empty_default():
    repo = InMemoryCaseManifestRepository()
    assert repo.for_case_id("UCD-FIL-001") is None


# ----- CaseManifestRow surface invariants -----


def test_row_columns_align_with_schema_constants():
    """Regression guard: if ``CASE_MANIFEST_COLUMNS`` grows or shrinks
    (Spec K-style schema migration), the dataclass needs a parallel
    update. This assertion makes the drift fail loudly."""
    field_names = set(CaseManifestRow.__dataclass_fields__.keys())
    schema_cols = set(CASE_MANIFEST_COLUMNS)
    assert field_names == schema_cols, (
        f"CaseManifestRow drift from CASE_MANIFEST_COLUMNS — "
        f"in row not in schema: {field_names - schema_cols}, "
        f"in schema not in row: {schema_cols - field_names}"
    )


def test_row_procedure_additional_is_tuple_not_list():
    """Frozen-dataclass discipline: ``procedure_additional`` is a tuple
    so equality / hash are stable. Catches a future refactor that
    accidentally swaps in a list."""
    row = CaseManifestRow.from_row({
        "ucd_fil_id": "UCD-FIL-001",
        "surgeon": "sarin",
        "case_year": "2026",
        "or_room": "OR 4",
        "procedure_primary": "Low anterior resection",
        "procedure_additional": '["Loop ileostomy"]',
        "approach": "Robotic",
        "conversion_target": "",
        "indication": "Colorectal cancer",
        "notes": "",
    })
    assert isinstance(row.procedure_additional, tuple)
