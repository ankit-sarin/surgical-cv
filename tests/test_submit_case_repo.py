"""Tests for ``CaseRepository.submit_case`` — ID allocation under flock,
manifest append via the CsvTable transaction, and ready-marker writing
(both CsvCaseRepository's real implementation and InMemoryCaseRepository's
test fake)."""

from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path

import pytest

from app.repos.cases import (
    CsvCaseRepository,
    InMemoryCaseRepository,
    RepoIntegrityError,
    SubmitError,
    SubmitResult,
    _next_ucd_fil_id,
    _write_ready_marker,
)


_HEADER = (
    "ucd_fil_id,surgeon,case_year,or_room,"
    "procedure_primary,procedure_additional,"
    "approach,conversion_target,"
    "indication,notes"
)


def _seed(path: Path, rows: list[str]) -> Path:
    path.write_text(_HEADER + "\n" + "\n".join(rows) + ("\n" if rows else ""))
    return path


def _valid_partial(**overrides) -> dict:
    base = {
        "surgeon": "sarin",
        "case_year": "2026",
        "or_room": "OR 4",
        "procedure_primary": "Sigmoidectomy",
        "procedure_additional": [],
        "approach": "Robotic",
        "conversion_target": "",
        "indication": "Colorectal cancer",
        "notes": "",
    }
    base.update(overrides)
    return base


def _submit(repo, partial: dict | None = None, segments: list[str] | None = None):
    """F-013 helper: derives ``expected_surgeon`` from ``partial['surgeon']``
    so happy-path tests don't have to thread the argument manually. Tests
    that exercise the F-013 mismatch guard call ``submit_case`` directly
    with intentionally divergent values."""
    if partial is None:
        partial = _valid_partial()
    if segments is None:
        segments = ["a.mp4"]
    return repo.submit_case(
        partial, segments, expected_surgeon=partial["surgeon"]
    )


# ----- _next_ucd_fil_id allocation logic -----


def test_next_id_empty_starts_at_001():
    assert _next_ucd_fil_id([]) == "UCD-FIL-001"


def test_next_id_max_plus_one():
    assert _next_ucd_fil_id(["UCD-FIL-001", "UCD-FIL-002"]) == "UCD-FIL-003"


def test_next_id_tolerates_gaps():
    """Spec: existing IDs 001/002/005 → next is 006 (max+1, not first-gap fill)."""
    assert _next_ucd_fil_id(
        ["UCD-FIL-001", "UCD-FIL-002", "UCD-FIL-005"]
    ) == "UCD-FIL-006"


def test_next_id_ignores_unrelated_ids():
    """Non-matching IDs don't participate in allocation."""
    assert _next_ucd_fil_id(["foo", "bar", "UCD-FIL-007"]) == "UCD-FIL-008"


def test_next_id_padding_remains_three_digits_through_999():
    assert _next_ucd_fil_id(["UCD-FIL-999"]) == "UCD-FIL-1000"


def test_next_id_orders_numerically_not_lexically():
    """Lexically '99' < '100', but numerically the inverse — allocator
    must compare integers."""
    assert _next_ucd_fil_id(
        ["UCD-FIL-099", "UCD-FIL-100"]
    ) == "UCD-FIL-101"


# ----- CsvCaseRepository.submit_case happy path -----


def test_csv_submit_appends_row_with_new_id(tmp_path):
    manifest = _seed(
        tmp_path / "m.csv",
        ["UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,,Robotic,,Colorectal cancer,"],
    )
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    (raw_root / "raw-sarin").mkdir()

    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)
    result = _submit(repo,
        _valid_partial(procedure_primary="Right hemicolectomy"),
        ["capt0_20260515-080000.mp4"],
    )

    assert isinstance(result, SubmitResult)
    assert result.ucd_fil_id == "UCD-FIL-002"
    assert result.submitted_at  # non-empty ISO 8601

    with manifest.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    new_row = rows[1]
    assert new_row["ucd_fil_id"] == "UCD-FIL-002"
    assert new_row["procedure_primary"] == "Right hemicolectomy"
    assert new_row["approach"] == "Robotic"
    assert new_row["conversion_target"] == ""
    assert new_row["procedure_additional"] == ""  # [] → "" on disk


def test_csv_submit_creates_manifest_when_missing(tmp_path):
    """First-ever submission must work on an empty NAS: no manifest, no
    raw folder yet."""
    manifest = tmp_path / "or-raw" / "case_manifest.csv"
    raw_root = tmp_path / "raw"
    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)

    result = _submit(repo,_valid_partial(), ["capt0_20260515-080000.mp4"])
    assert result.ucd_fil_id == "UCD-FIL-001"
    assert manifest.exists()
    with manifest.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["ucd_fil_id"] == "UCD-FIL-001"


def test_csv_submit_serializes_procedure_additional_as_json(tmp_path):
    manifest = _seed(tmp_path / "m.csv", [])
    raw_root = tmp_path / "raw"
    raw_root.mkdir()

    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)
    _submit(repo,
        _valid_partial(
            procedure_primary="Right hemicolectomy",
            procedure_additional=["TAMIS"],
        ),
        ["a.mp4"],
    )

    with manifest.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["procedure_additional"] == '["TAMIS"]'


def test_csv_submit_drops_ready_marker(tmp_path):
    manifest = _seed(tmp_path / "m.csv", [])
    raw_root = tmp_path / "raw"
    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)

    segments = ["capt0_20260515-080000.mp4", "capt0_20260515-083000.mp4"]
    result = _submit(repo,_valid_partial(), segments)

    marker = raw_root / "raw-sarin" / f".ready-{result.ucd_fil_id}.json"
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["ucd_fil_id"] == result.ucd_fil_id
    assert payload["surgeon"] == "sarin"
    assert payload["segments"] == segments
    assert payload["submitted_at"] == result.submitted_at


def test_csv_submit_marker_is_dot_prefixed(tmp_path):
    """Spec: dot-prefix hides the marker from BDV / Citrix views."""
    manifest = _seed(tmp_path / "m.csv", [])
    repo = CsvCaseRepository(manifest, raw_video_root=tmp_path / "raw")
    result = _submit(repo,_valid_partial(), ["a.mp4"])
    marker = tmp_path / "raw" / "raw-sarin" / f".ready-{result.ucd_fil_id}.json"
    assert marker.name.startswith(".")


def test_csv_submit_marker_in_correct_surgeon_folder(tmp_path):
    """The ready marker goes to raw-{surgeon}/, not raw-{any-other}/."""
    manifest = _seed(tmp_path / "m.csv", [])
    repo = CsvCaseRepository(manifest, raw_video_root=tmp_path / "raw")
    _submit(repo,_valid_partial(surgeon="miller"), ["a.mp4"])
    assert (tmp_path / "raw" / "raw-miller").exists()
    assert not (tmp_path / "raw" / "raw-sarin").exists()


def test_csv_submit_normalizes_none_conversion_target(tmp_path):
    """gr.State carries None for "no conversion"; repo must store "" on disk."""
    manifest = _seed(tmp_path / "m.csv", [])
    repo = CsvCaseRepository(manifest, raw_video_root=tmp_path / "raw")
    _submit(repo,
        _valid_partial(conversion_target=None), ["a.mp4"]
    )
    with manifest.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["conversion_target"] == ""


def test_csv_submit_normalizes_none_notes(tmp_path):
    manifest = _seed(tmp_path / "m.csv", [])
    repo = CsvCaseRepository(manifest, raw_video_root=tmp_path / "raw")
    _submit(repo,_valid_partial(notes=None), ["a.mp4"])
    with manifest.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["notes"] == ""


# ----- CsvCaseRepository.submit_case failure paths -----


def test_csv_submit_raises_submit_error_on_invalid_picklist_value(tmp_path):
    """Validation should have caught this upstream, but the repo defends in
    depth — invalid procedure trips Pydantic and surfaces as SubmitError."""
    manifest = _seed(tmp_path / "m.csv", [])
    repo = CsvCaseRepository(manifest, raw_video_root=tmp_path / "raw")
    with pytest.raises(SubmitError):
        _submit(repo,
            _valid_partial(procedure_primary=""),  # min_length=1 fails
            ["a.mp4"],
        )


# ----- F-013: surgeon-mismatch defense-in-depth guard -----


def test_csv_submit_raises_repointegrity_on_surgeon_mismatch(tmp_path):
    """F-013: caller-supplied partial_row['surgeon'] must equal
    expected_surgeon. Mismatch raises RepoIntegrityError with both values
    in the message — distinct from SubmitError so a future caller bug
    surfaces loudly rather than as a polite UI error."""
    manifest = _seed(tmp_path / "m.csv", [])
    raw_root = tmp_path / "raw"
    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)

    with pytest.raises(RepoIntegrityError) as exc_info:
        repo.submit_case(
            _valid_partial(surgeon="miller"),
            ["a.mp4"],
            expected_surgeon="sarin",
        )
    msg = str(exc_info.value)
    assert "sarin" in msg
    assert "miller" in msg


def test_csv_submit_mismatch_writes_no_state(tmp_path):
    """F-013: a mismatched submit must NOT touch the manifest, NOT write a
    ready marker, NOT acquire the flock, and NOT create any sibling files
    (.lock, .tmp). The whole operation aborts before any I/O."""
    manifest = _seed(tmp_path / "m.csv", [])
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)

    manifest_bytes_before = manifest.read_bytes()
    with pytest.raises(RepoIntegrityError):
        repo.submit_case(
            _valid_partial(surgeon="miller"),
            ["a.mp4"],
            expected_surgeon="sarin",
        )

    # Manifest untouched (byte-identical, including the ".lock" sibling
    # would be created by CsvTable.transaction if reached — assert absent).
    assert manifest.read_bytes() == manifest_bytes_before
    assert not (manifest.parent / (manifest.name + ".lock")).exists()
    # Marker file absent in both surgeon folders.
    assert not (raw_root / "raw-sarin").exists() or not list(
        (raw_root / "raw-sarin").glob(".ready-*.json")
    )
    assert not (raw_root / "raw-miller").exists() or not list(
        (raw_root / "raw-miller").glob(".ready-*.json")
    )


def test_inmem_submit_also_enforces_surgeon_assertion(tmp_path):
    """F-013: the in-memory test fake mirrors the production guard so tests
    that exercise the fake see the same fail-fast behavior. If they
    diverged, a test could pass against the fake and fail in production."""
    repo = InMemoryCaseRepository()
    with pytest.raises(RepoIntegrityError):
        repo.submit_case(
            _valid_partial(surgeon="miller"),
            ["a.mp4"],
            expected_surgeon="sarin",
        )
    # No row inserted into the fake's dict.
    assert repo.list_owned_by("sarin") == []
    assert repo.list_owned_by("miller") == []


# ----- _write_ready_marker primitive -----


def test_write_ready_marker_creates_raw_dir_if_missing(tmp_path):
    raw_root = tmp_path / "raw"
    p = _write_ready_marker(
        raw_root, "sarin", "UCD-FIL-005", "2026-05-15T08:00:00+00:00",
        ["a.mp4", "b.mp4"],
    )
    assert p.exists()
    assert p.parent == raw_root / "raw-sarin"


def test_write_ready_marker_payload_shape(tmp_path):
    p = _write_ready_marker(
        tmp_path, "sarin", "UCD-FIL-005", "2026-05-15T08:00:00+00:00",
        ["seg1.mp4", "seg2.mp4"],
    )
    payload = json.loads(p.read_text())
    assert payload == {
        "ucd_fil_id": "UCD-FIL-005",
        "surgeon": "sarin",
        "submitted_at": "2026-05-15T08:00:00+00:00",
        "segments": ["seg1.mp4", "seg2.mp4"],
    }


def test_write_ready_marker_overwrites_existing(tmp_path):
    """Idempotent re-submit (e.g., retry after a marker-only failure)
    must replace the old marker, not raise."""
    raw_root = tmp_path
    (raw_root / "raw-sarin").mkdir()
    target = raw_root / "raw-sarin" / ".ready-UCD-FIL-005.json"
    target.write_text("stale contents")
    _write_ready_marker(
        raw_root, "sarin", "UCD-FIL-005", "2026-05-15T08:00:00+00:00",
        ["fresh.mp4"],
    )
    payload = json.loads(target.read_text())
    assert payload["segments"] == ["fresh.mp4"]


def test_write_ready_marker_no_tmp_files_left_behind(tmp_path):
    """The atomic-rename leaves only the final marker — temp files must
    not linger in the surgeon's raw folder."""
    _write_ready_marker(
        tmp_path, "sarin", "UCD-FIL-005", "2026-05-15T08:00:00+00:00",
        ["a.mp4"],
    )
    leftovers = [
        p for p in (tmp_path / "raw-sarin").iterdir()
        if p.name != ".ready-UCD-FIL-005.json"
    ]
    assert leftovers == []


# ----- InMemoryCaseRepository.submit_case -----


def test_inmem_submit_allocates_first_id():
    repo = InMemoryCaseRepository()
    result = _submit(repo,_valid_partial(), ["a.mp4"])
    assert result.ucd_fil_id == "UCD-FIL-001"


def test_inmem_submit_sequential_allocations_increment():
    repo = InMemoryCaseRepository()
    r1 = _submit(repo,_valid_partial(), ["a.mp4"])
    r2 = _submit(repo,_valid_partial(), ["b.mp4"])
    r3 = _submit(repo,_valid_partial(), ["c.mp4"])
    assert [r1.ucd_fil_id, r2.ucd_fil_id, r3.ucd_fil_id] == [
        "UCD-FIL-001", "UCD-FIL-002", "UCD-FIL-003",
    ]


def test_inmem_submit_preserves_segments_in_record():
    repo = InMemoryCaseRepository()
    result = _submit(repo,_valid_partial(), ["seg1.mp4", "seg2.mp4"])
    case = repo.get_case(result.ucd_fil_id)
    assert case["segments"] == ["seg1.mp4", "seg2.mp4"]


def test_inmem_submit_seeds_listable_for_owning_surgeon():
    """Right after submission the case must show up in list_owned_by."""
    repo = InMemoryCaseRepository()
    result = _submit(repo,_valid_partial(surgeon="sarin"), ["a.mp4"])
    assert result.ucd_fil_id in repo.list_owned_by("sarin")


def test_inmem_submit_respects_preseeded_ids():
    """If the repo was pre-seeded with UCD-FIL-005, the next submit lands
    at 006 (allocator sees the pre-seed)."""
    repo = InMemoryCaseRepository({
        "UCD-FIL-005": {"surgeon": "sarin"},
    })
    result = _submit(repo,_valid_partial(), ["a.mp4"])
    assert result.ucd_fil_id == "UCD-FIL-006"


# ----- Concurrent submits — flock correctness (best-effort threading) -----


def test_csv_concurrent_submits_get_distinct_ids(tmp_path):
    """Two threads each submit a case at the same time. Under flock, both
    must succeed and receive distinct IDs. This is best-effort; flake
    tolerance is OK — production correctness rests on flock semantics."""
    manifest = _seed(tmp_path / "m.csv", [])
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    repo = CsvCaseRepository(manifest, raw_video_root=raw_root)

    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait(timeout=5)
            r = _submit(repo,_valid_partial(), ["a.mp4"])
            results.append(r.ucd_fil_id)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert errors == []
    assert len(results) == 2
    assert results[0] != results[1]
    assert set(results) == {"UCD-FIL-001", "UCD-FIL-002"}
