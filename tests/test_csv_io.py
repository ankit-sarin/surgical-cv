import csv
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipeline import csv_io
from pipeline.csv_io import (
    CorruptCsvError,
    CsvIoError,
    CsvTable,
    DuplicateRowError,
    RowNotFoundError,
)
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_state_row(ucd_fil_id="UCD-FIL-001", **overrides):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        raw_segments=["a.mp4", "b.mp4"],
        concat_filename="",
        deid_filename="",
        stage=Stage.intake,
        concat_ts="",
        deid_ts="",
        verify_ts="",
        verification_notes="",
    )
    base.update(overrides)
    return PipelineStateRow(**base)


def _make_manifest_row(ucd_fil_id="UCD-FIL-001", **overrides):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        surgeon="sarin",
        case_year="2026",
        or_room="OR4",
        procedure_name="Sigmoidectomy",
        approach="Robotic",
        indication="Diverticulitis",
        notes="",
    )
    base.update(overrides)
    return CaseManifestRow(**base)


def _state_table(path):
    return CsvTable(path, PIPELINE_STATE_COLUMNS, PipelineStateRow)


def _manifest_table(path):
    return CsvTable(path, CASE_MANIFEST_COLUMNS, CaseManifestRow)


def test_snapshot_returns_empty_on_missing_file(tmp_path):
    t = _state_table(tmp_path / "state.csv")
    assert t.snapshot() == []


def test_transaction_on_missing_file_enters_empty(tmp_path):
    t = _state_table(tmp_path / "state.csv")
    with t.transaction() as tx:
        assert tx.read_all() == []
    assert not (tmp_path / "state.csv").exists()


def test_append_writes_header_and_row(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row())
    text = path.read_text()
    lines = text.splitlines()
    assert lines[0] == ",".join(PIPELINE_STATE_COLUMNS)
    assert len(lines) == 2


def test_round_trip_preserves_all_fields(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    original = _make_state_row(
        stage=Stage.verified,
        concat_filename="UCD-FIL-001_raw.mp4",
        deid_filename="UCD-FIL-001_video.mp4",
        concat_ts="2026-05-12T09:30:00",
        deid_ts="2026-05-12T10:15:00",
        verify_ts="2026-05-12T10:45:00",
        verification_notes="all good",
    )
    with t.transaction() as tx:
        tx.append(original)
    snap = t.snapshot()
    assert len(snap) == 1
    assert snap[0] == original


def test_manifest_round_trip_preserves_empty_notes(tmp_path):
    path = tmp_path / "manifest.csv"
    t = _manifest_table(path)
    original = _make_manifest_row(notes="")
    with t.transaction() as tx:
        tx.append(original)
    snap = t.snapshot()
    assert snap[0] == original
    assert snap[0].notes == ""


def test_duplicate_append_raises(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row())
    with pytest.raises(DuplicateRowError) as exc_info:
        with t.transaction() as tx:
            tx.append(_make_state_row())
    assert exc_info.value.ucd_fil_id == "UCD-FIL-001"
    assert "UCD-FIL-001" in str(exc_info.value)


def test_update_modifies_only_targeted_row(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row("UCD-FIL-001"))
        tx.append(_make_state_row("UCD-FIL-002"))
    with t.transaction() as tx:
        returned = tx.update(
            "UCD-FIL-001",
            stage=Stage.concatenated,
            concat_filename="UCD-FIL-001_raw.mp4",
            concat_ts="2026-05-12T10:00:00",
        )
    assert returned.stage == Stage.concatenated
    snap = {r.ucd_fil_id: r for r in t.snapshot()}
    assert snap["UCD-FIL-001"].stage == Stage.concatenated
    assert snap["UCD-FIL-001"].concat_filename == "UCD-FIL-001_raw.mp4"
    assert snap["UCD-FIL-001"].concat_ts == "2026-05-12T10:00:00"
    assert snap["UCD-FIL-002"].stage == Stage.intake
    assert snap["UCD-FIL-002"].concat_filename == ""


def test_update_missing_id_raises(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row("UCD-FIL-001"))
    with pytest.raises(RowNotFoundError) as exc_info:
        with t.transaction() as tx:
            tx.update("UCD-FIL-999", stage=Stage.failed)
    assert exc_info.value.ucd_fil_id == "UCD-FIL-999"


def test_update_unknown_field_raises_clear_error(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row())
    with pytest.raises(ValueError) as exc_info:
        with t.transaction() as tx:
            tx.update("UCD-FIL-001", not_a_real_field="x")
    msg = str(exc_info.value)
    assert "not_a_real_field" in msg
    assert "PipelineStateRow" in msg


def test_exception_in_transaction_leaves_file_untouched(tmp_path):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row("UCD-FIL-001"))
    before = path.read_text()
    before_mtime = path.stat().st_mtime_ns
    time.sleep(0.01)
    with pytest.raises(RuntimeError):
        with t.transaction() as tx:
            tx.append(_make_state_row("UCD-FIL-002"))
            raise RuntimeError("boom")
    assert path.read_text() == before
    snap = t.snapshot()
    assert len(snap) == 1
    assert snap[0].ucd_fil_id == "UCD-FIL-001"


def test_corrupt_header_raises_on_transaction_and_snapshot(tmp_path):
    path = tmp_path / "state.csv"
    path.write_text("wrong,header,columns\n")
    t = _state_table(path)
    with pytest.raises(CorruptCsvError) as exc_info:
        t.snapshot()
    assert "header mismatch" in str(exc_info.value)
    assert exc_info.value.row_number is None
    with pytest.raises(CorruptCsvError):
        with t.transaction() as _:
            pass


def test_corrupt_row_in_otherwise_valid_file(tmp_path):
    path = tmp_path / "state.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(PIPELINE_STATE_COLUMNS), lineterminator="\n")
        w.writeheader()
        w.writerow(_make_state_row("UCD-FIL-001").to_csv_dict())
        bad = _make_state_row("UCD-FIL-002").to_csv_dict()
        bad["ucd_fil_id"] = "NOT-A-VALID-ID"
        w.writerow(bad)
    t = _state_table(path)
    with pytest.raises(CorruptCsvError) as exc_info:
        t.snapshot()
    assert exc_info.value.row_number == 2
    assert exc_info.value.path == path


def test_corrupt_csv_error_subclasses_csv_io_error(tmp_path):
    assert issubclass(CorruptCsvError, CsvIoError)
    assert issubclass(RowNotFoundError, CsvIoError)
    assert issubclass(DuplicateRowError, CsvIoError)


def test_os_replace_failure_cleans_up_tmp_and_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "state.csv"
    t = _state_table(path)
    with t.transaction() as tx:
        tx.append(_make_state_row("UCD-FIL-001"))
    before = path.read_text()

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(csv_io.os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        with t.transaction() as tx:
            tx.append(_make_state_row("UCD-FIL-002"))

    assert path.read_text() == before
    leftover = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == [], f"tmp file leaked: {leftover}"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="fcntl.flock not available on Windows",
)
def test_concurrent_appends_are_serialized(tmp_path):
    path = tmp_path / "state.csv"
    worker = f"""
import sys, time
from pathlib import Path
from pipeline.csv_io import CsvTable
from pipeline.schemas import PipelineStateRow, Stage, PIPELINE_STATE_COLUMNS

ucd_id = sys.argv[1]
hold = float(sys.argv[2])
t = CsvTable(Path({str(path)!r}), PIPELINE_STATE_COLUMNS, PipelineStateRow)
with t.transaction() as tx:
    time.sleep(hold)
    tx.append(PipelineStateRow(
        ucd_fil_id=ucd_id,
        raw_segments=['a.mp4'],
        concat_filename='', deid_filename='',
        stage=Stage.intake,
        concat_ts='', deid_ts='', verify_ts='', verification_notes='',
    ))
"""
    started = time.time()
    p1 = subprocess.Popen(
        [sys.executable, "-c", worker, "UCD-FIL-001", "0.4"],
        cwd=str(PROJECT_ROOT),
    )
    p2 = subprocess.Popen(
        [sys.executable, "-c", worker, "UCD-FIL-002", "0.4"],
        cwd=str(PROJECT_ROOT),
    )
    rc1 = p1.wait(timeout=15)
    rc2 = p2.wait(timeout=15)
    elapsed = time.time() - started
    assert rc1 == 0
    assert rc2 == 0
    assert elapsed >= 0.7, (
        f"transactions ran in parallel (elapsed={elapsed:.2f}s); lock did not serialize"
    )

    text = path.read_text()
    lines = text.splitlines()
    assert lines[0] == ",".join(PIPELINE_STATE_COLUMNS)
    assert len(lines) == 3
    t = _state_table(path)
    snap = t.snapshot()
    ids = {r.ucd_fil_id for r in snap}
    assert ids == {"UCD-FIL-001", "UCD-FIL-002"}
