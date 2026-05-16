"""Tests for the per-case ``phi_redacted`` attention-item emit wired
into ``dispatch_marker`` (Brief #3.5b).

The flow under test:

  1. Worker calls ``redact_case_notes`` which scrubs PHI in the
     manifest's notes column and returns a category-count dict.
  2. If the dict is non-empty, the worker upserts a single
     ``phi_redacted`` attention_items row keyed by (case_id, type)
     via :meth:`SqliteAttentionItemsRepository.upsert_by_case_and_type`.
  3. Retry of the same marker (operator moves it back from
     ``.failed/``) reads already-scrubbed notes → empty dict → no
     duplicate row.

Load-bearing properties exercised here:

  - **PHI-safe details**: the row's ``details`` column names
    categories only, never values. Sentinel PHI fixtures
    (``Zxqv Wmrp`` / ``99887766`` / ``1/14/1962``) must NOT appear
    anywhere in the persisted ``details``.
  - **Idempotency**: re-running with already-scrubbed notes leaves
    exactly one row, untouched.
  - **Coalesce on re-emit**: if the worker re-emits with a
    different category mix (e.g. operator restored notes with new
    PHI), the same row is updated in place — details/severity are
    refreshed and ``updated_at`` advances past ``created_at``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)

from app.db.connection import connect, utcnow
from app.worker.dispatch import (
    SubprocessResult,
    dispatch_marker,
)
from app.worker.failures import ensure_system_worker_user
from app.worker.scan import Marker


_SEED_TS = "2026-05-15T00:00:00+00:00"

# Sentinel PHI — distinct, hard-to-confuse values so the
# "details must not contain the raw value" assertions can't
# false-positive on common substrings.
_PHI_NOTES = (
    "Pt. Zxqv Wmrp MRN: 99887766, dob 1/14/1962. "
    "Sigmoid resection planned."
)
_PHI_RAW_VALUES = ("Zxqv Wmrp", "99887766", "1/14/1962")


# ----- fixtures -----


def _init_app_db(db_path: Path) -> None:
    """Apply schema.sql + seed the minimum users needed for the emit
    path: ``asarin`` (surgeon with folder_slug='sarin') as the
    affected_user FK target, and ``system_worker`` for the
    ``created_by`` FK target."""
    schema = (
        Path(__file__).resolve().parent.parent
        / "app" / "db" / "schema.sql"
    ).read_text()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO specialties (specialty_code, display_name, "
            " active, created_at) VALUES (?, ?, 1, ?)",
            ("colorectal", "Colorectal Surgery", _SEED_TS),
        )
        conn.execute(
            "INSERT INTO users (username, role, folder_slug, specialty, "
            " active, created_at) VALUES "
            "(?, 'surgeon', 'sarin', 'colorectal', 1, ?)",
            ("asarin", _SEED_TS),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def app_db(tmp_path, monkeypatch) -> Path:
    db = tmp_path / "test.db"
    _init_app_db(db)
    monkeypatch.setenv("APP_DB_PATH", str(db))
    # system_worker row is upserted on demand — call it once so the
    # created_by FK on attention_items is satisfied. Worker-main does
    # this at startup; tests skipping --once boot do it explicitly.
    ensure_system_worker_user()
    return db


@pytest.fixture
def paths(tmp_path, app_db) -> NasPaths:
    or_raw = tmp_path / "or-raw"
    or_raw.mkdir()
    return NasPaths(
        root=tmp_path,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )


@pytest.fixture
def marker(tmp_path) -> Marker:
    raw = tmp_path / "raw-sarin"
    raw.mkdir()
    return Marker(
        path=raw / ".ready-UCD-FIL-005.json",
        ucd_fil_id="UCD-FIL-005",
        surgeon="sarin",
        submitted_at="2026-05-15T08:00:00+00:00",
        segments=["capt0_20260515-080000.mp4"],
    )


def _seed_manifest(paths: NasPaths, case_id: str, surgeon: str, notes: str):
    table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    with table.transaction() as tx:
        tx.append(CaseManifestRow(
            ucd_fil_id=case_id,
            surgeon=surgeon,
            case_year="2026",
            or_room="OR 4",
            procedure_primary="Sigmoidectomy",
            procedure_additional=[],
            approach="Robotic",
            conversion_target="",
            indication="Colorectal cancer",
            notes=notes,
        ))


def _set_state_stage(paths: NasPaths, case_id: str, stage: Stage, **fields):
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    with table.transaction() as tx:
        if tx.find(case_id) is None:
            return
        tx.update(case_id, stage=stage, **fields)


class _SuccessDriver:
    """Minimal stub that advances the state CSV through all three
    stages without emitting subprocess output. The PHI emit happens
    BEFORE the first stage — driver behavior shouldn't affect it,
    but a full-success path keeps the test focused on the emit and
    avoids tangential hard_fail noise."""

    def __init__(self, paths: NasPaths, case_id: str):
        self._paths = paths
        self._case = case_id

    def concat(self, surgeon: str, case_id: str) -> SubprocessResult:
        _set_state_stage(
            self._paths, case_id, Stage.concatenated,
            concat_filename="sarin_20260515-080000.mp4",
            concat_ts="2026-05-15T08:00:00",
        )
        return SubprocessResult(returncode=0, stdout="", stderr="")

    def deid(self, surgeon: str, case_id: str) -> SubprocessResult:
        _set_state_stage(
            self._paths, case_id, Stage.deidentified,
            deid_filename=f"{case_id}_video.mp4",
            deid_ts="2026-05-15T08:30:00",
        )
        return SubprocessResult(returncode=0, stdout="", stderr="")

    def verify(self, surgeon: str, case_id: str) -> SubprocessResult:
        _set_state_stage(
            self._paths, case_id, Stage.verified,
            verify_ts="2026-05-15T08:45:00",
            verification_notes="all checks passed",
        )
        return SubprocessResult(returncode=0, stdout="", stderr="")


def _attention_rows(db: Path, case_id: str) -> list[dict]:
    conn = connect(db)
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM attention_items WHERE case_id = ? "
                "ORDER BY id",
                (case_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ----- 1. PHI found → exactly one item, severity='normal' -----


def test_phi_present_emits_exactly_one_item_severity_normal(
    paths, marker, app_db,
):
    _seed_manifest(paths, marker.ucd_fil_id, marker.surgeon, _PHI_NOTES)
    driver = _SuccessDriver(paths, marker.ucd_fil_id)

    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "success"

    rows = _attention_rows(app_db, marker.ucd_fil_id)
    assert len(rows) == 1, (
        f"expected exactly one phi_redacted row, got {len(rows)}: {rows!r}"
    )
    row = rows[0]
    assert row["type"] == "phi_redacted"
    assert row["severity"] == "normal"
    assert row["status"] == "open"
    assert row["affected_user"] == "asarin"  # folder_slug=sarin → asarin
    assert row["created_by"] == "system_worker"


# ----- 2. PHI-safe details (load-bearing) -----


def test_emitted_details_never_contain_raw_phi_values(
    paths, marker, app_db,
):
    """The row's ``details`` must name categories only, never the
    raw PHI strings. This is the single most load-bearing property
    of the rollup emit — a regression here surfaces raw patient
    data in the surgeon-visible Action Required tab."""
    _seed_manifest(paths, marker.ucd_fil_id, marker.surgeon, _PHI_NOTES)
    driver = _SuccessDriver(paths, marker.ucd_fil_id)

    dispatch_marker(marker, paths, driver)

    rows = _attention_rows(app_db, marker.ucd_fil_id)
    assert len(rows) == 1
    details = rows[0]["details"] or ""
    for raw_value in _PHI_RAW_VALUES:
        assert raw_value not in details, (
            f"raw PHI value {raw_value!r} leaked into details: "
            f"{details!r}"
        )
    # Positive shape: the canonical sentence + at least one category
    # word. Category vocabulary is the source of truth; assert on
    # the structural prefix + presence of expected categories.
    assert details.startswith("PHI was found and redacted. Categories: ")
    assert details.endswith(".")
    for expected_cat in ("mrn", "name", "date"):
        assert expected_cat in details, (
            f"expected category {expected_cat!r} missing from "
            f"details: {details!r}"
        )


# ----- 3. Zero PHI → no item -----


def test_clean_notes_emit_no_attention_item(paths, marker, app_db):
    _seed_manifest(
        paths, marker.ucd_fil_id, marker.surgeon,
        "sigmoidectomy, uneventful",
    )
    driver = _SuccessDriver(paths, marker.ucd_fil_id)

    dispatch_marker(marker, paths, driver)

    rows = _attention_rows(app_db, marker.ucd_fil_id)
    assert rows == [], (
        f"expected no attention items for clean notes, got: {rows!r}"
    )


# ----- 4. Idempotency on retry → exactly one row -----


def test_retry_after_scrub_leaves_exactly_one_row(
    paths, marker, app_db,
):
    """Operator re-triggers a case by moving its marker back from
    ``.failed/``. The notes column is already scrubbed from the
    first run, so the second pass yields ``{}`` from the scanner
    and the emit is skipped — leaving the original row untouched."""
    _seed_manifest(paths, marker.ucd_fil_id, marker.surgeon, _PHI_NOTES)
    driver = _SuccessDriver(paths, marker.ucd_fil_id)

    dispatch_marker(marker, paths, driver)
    rows_first = _attention_rows(app_db, marker.ucd_fil_id)
    assert len(rows_first) == 1
    first_id = rows_first[0]["id"]
    first_updated_at = rows_first[0]["updated_at"]

    # Second pass — same marker, manifest already scrubbed.
    # pipeline_state row is still at stage=verified; the driver's
    # state-advance side-effects no-op cleanly. We don't reset state
    # here because dispatch_marker doesn't gate on it — the PHI emit
    # is the only assertion target.
    dispatch_marker(marker, paths, driver)

    rows_second = _attention_rows(app_db, marker.ucd_fil_id)
    assert len(rows_second) == 1, (
        f"retry left {len(rows_second)} rows; expected 1: "
        f"{rows_second!r}"
    )
    # Same row, untouched. The id is stable because no UPSERT
    # conflict fired (the second scan returned {} → no upsert call).
    assert rows_second[0]["id"] == first_id
    assert rows_second[0]["updated_at"] == first_updated_at


# ----- 5. Different categories on re-run → coalesce + advance updated_at -----


def test_reemit_with_new_categories_updates_in_place(
    paths, marker, app_db, monkeypatch,
):
    """If the operator restored notes with a DIFFERENT PHI mix
    between runs (e.g. corrected a typo and reintroduced fresh
    identifiers), the worker re-emits. The schema's partial unique
    index forces the upsert to coalesce onto the same row —
    details + severity refresh, ``updated_at`` advances past
    ``created_at``, ``id`` and ``created_at`` stay frozen."""
    # Monkeypatch utcnow inside the repo so the second emit's
    # timestamp is strictly greater than the first. utcnow() is
    # second-precision; relying on wall-clock drift would force a
    # 1-second sleep into the test.
    ts_iter = iter([
        "2026-05-15T08:00:00+00:00",
        "2026-05-15T08:00:01+00:00",
    ])
    real_utcnow = utcnow

    def _ticking_utcnow():
        try:
            return next(ts_iter)
        except StopIteration:
            return real_utcnow()

    monkeypatch.setattr(
        "app.repos.attention.utcnow", _ticking_utcnow,
    )

    # First emit: notes with name + mrn + date.
    _seed_manifest(paths, marker.ucd_fil_id, marker.surgeon, _PHI_NOTES)
    driver = _SuccessDriver(paths, marker.ucd_fil_id)
    dispatch_marker(marker, paths, driver)

    rows_first = _attention_rows(app_db, marker.ucd_fil_id)
    assert len(rows_first) == 1
    first = rows_first[0]
    first_id = first["id"]
    first_created_at = first["created_at"]
    first_updated_at = first["updated_at"]
    first_details = first["details"]

    # Simulate the operator restoring fresh PHI (different category
    # mix — phone instead of date).
    table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    with table.transaction() as tx:
        tx.update(
            marker.ucd_fil_id,
            notes="Patient: Aaaa Bbbb. Call (415) 555-1212 for followup.",
        )

    dispatch_marker(marker, paths, driver)

    rows_second = _attention_rows(app_db, marker.ucd_fil_id)
    assert len(rows_second) == 1, (
        f"re-emit produced {len(rows_second)} rows; index should "
        f"have coalesced to 1: {rows_second!r}"
    )
    second = rows_second[0]
    # Same row identity, frozen creation metadata.
    assert second["id"] == first_id
    assert second["created_at"] == first_created_at
    # Mutable columns refreshed.
    assert second["details"] != first_details
    assert second["updated_at"] > first_updated_at
    # And the new details still carry no raw PHI.
    new_details = second["details"] or ""
    for raw_value in ("Aaaa Bbbb", "415", "555-1212"):
        assert raw_value not in new_details, (
            f"raw PHI value {raw_value!r} leaked into refreshed "
            f"details: {new_details!r}"
        )
