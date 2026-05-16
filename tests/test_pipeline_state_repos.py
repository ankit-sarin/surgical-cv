"""Tests for ``app/repos/pipeline_state.py`` — CsvPipelineStateRepository
against a tmpdir CSV fixture, plus InMemoryPipelineStateRepository's
pure-Python behavior. Mirrors the layout of ``tests/test_repos.py`` so
the two read paths stay symmetric."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.repos.pipeline_state import (
    CsvPipelineStateRepository,
    InMemoryPipelineStateRepository,
    state_path,
)
from pipeline.schemas import PIPELINE_STATE_COLUMNS, PipelineStateRow, Stage


_HEADER = ",".join(PIPELINE_STATE_COLUMNS)


def _row(
    ucd_fil_id: str = "UCD-FIL-001",
    raw_segments: str = "a.mp4|b.mp4",
    concat_filename: str = "",
    deid_filename: str = "",
    stage: str = "intake",
    intake_ts: str = "",
    concat_ts: str = "",
    deid_ts: str = "",
    verify_ts: str = "",
    verification_notes: str = "",
) -> str:
    return ",".join([
        ucd_fil_id,
        raw_segments,
        concat_filename,
        deid_filename,
        stage,
        intake_ts,
        concat_ts,
        deid_ts,
        verify_ts,
        verification_notes,
    ])


def _write_state_csv(target: Path, rows: list[str]) -> Path:
    target.write_text(_HEADER + "\n" + "\n".join(rows) + ("\n" if rows else ""))
    return target


# ----- state_path() env override -----


def test_state_path_honors_env(monkeypatch, tmp_path):
    custom = tmp_path / "custom_state.csv"
    monkeypatch.setenv("PIPELINE_STATE_PATH", str(custom))
    assert state_path() == custom


def test_state_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_STATE_PATH", raising=False)
    p = state_path()
    assert p.name == "pipeline_state.csv"
    assert "/mnt/nas/or-raw" in str(p)


# ----- CsvPipelineStateRepository — happy paths -----


def test_csv_list_for_case_ids_returns_matching_subset(tmp_path):
    state = _write_state_csv(
        tmp_path / "s.csv",
        [
            _row("UCD-FIL-001", stage="verified",
                 verify_ts="2026-05-12T10:45:00"),
            _row("UCD-FIL-002", stage="deidentified",
                 deid_ts="2026-05-12T10:15:00"),
            _row("UCD-FIL-099", stage="intake",
                 intake_ts="2026-05-15T08:00:00+00:00"),
        ],
    )
    repo = CsvPipelineStateRepository(state)
    out = repo.list_for_case_ids(["UCD-FIL-001", "UCD-FIL-002"])
    assert set(out.keys()) == {"UCD-FIL-001", "UCD-FIL-002"}
    assert out["UCD-FIL-001"]["stage"] == Stage.verified
    assert out["UCD-FIL-002"]["stage"] == Stage.deidentified


def test_csv_list_for_case_ids_unknown_ids_absent(tmp_path):
    state = _write_state_csv(
        tmp_path / "s.csv",
        [_row("UCD-FIL-001", stage="verified")],
    )
    repo = CsvPipelineStateRepository(state)
    out = repo.list_for_case_ids(["UCD-FIL-001", "UCD-FIL-999"])
    assert "UCD-FIL-001" in out
    assert "UCD-FIL-999" not in out


def test_csv_list_for_case_ids_empty_input_returns_empty(tmp_path):
    state = _write_state_csv(
        tmp_path / "s.csv",
        [_row("UCD-FIL-001", stage="verified")],
    )
    repo = CsvPipelineStateRepository(state)
    assert repo.list_for_case_ids([]) == {}


def test_csv_get_state_happy_path(tmp_path):
    state = _write_state_csv(
        tmp_path / "s.csv",
        [
            _row(
                "UCD-FIL-001",
                stage="verified",
                concat_filename="sarin_20260512-093000.mp4",
                deid_filename="UCD-FIL-001_video.mp4",
                intake_ts="2026-05-12T09:25:00+00:00",
                concat_ts="2026-05-12T09:30:00",
                deid_ts="2026-05-12T10:15:00",
                verify_ts="2026-05-12T10:45:00",
                verification_notes="all good",
            )
        ],
    )
    repo = CsvPipelineStateRepository(state)
    s = repo.get_state("UCD-FIL-001")
    assert s is not None
    assert s["ucd_fil_id"] == "UCD-FIL-001"
    assert s["stage"] == Stage.verified
    assert s["raw_segments"] == ["a.mp4", "b.mp4"]
    assert s["concat_filename"] == "sarin_20260512-093000.mp4"
    assert s["deid_filename"] == "UCD-FIL-001_video.mp4"
    assert s["intake_ts"] == "2026-05-12T09:25:00+00:00"
    assert s["concat_ts"] == "2026-05-12T09:30:00"
    assert s["deid_ts"] == "2026-05-12T10:15:00"
    assert s["verify_ts"] == "2026-05-12T10:45:00"
    assert s["verification_notes"] == "all good"


def test_csv_get_state_unknown_returns_none(tmp_path):
    state = _write_state_csv(tmp_path / "s.csv", [])
    repo = CsvPipelineStateRepository(state)
    assert repo.get_state("UCD-FIL-001") is None


# ----- CsvPipelineStateRepository — missing file -----


def test_csv_missing_file_yields_empty_results(tmp_path):
    repo = CsvPipelineStateRepository(tmp_path / "does-not-exist.csv")
    assert repo.list_for_case_ids(["UCD-FIL-001"]) == {}
    assert repo.get_state("UCD-FIL-001") is None


# ----- env-var-resolved path (no explicit constructor arg) -----


def test_csv_env_var_path(monkeypatch, tmp_path):
    state = _write_state_csv(
        tmp_path / "s.csv",
        [_row("UCD-FIL-001", stage="verified")],
    )
    monkeypatch.setenv("PIPELINE_STATE_PATH", str(state))
    repo = CsvPipelineStateRepository()  # no explicit path → reads env
    assert "UCD-FIL-001" in repo.list_for_case_ids(["UCD-FIL-001"])


# ----- stateless re-read -----


def test_csv_reads_fresh_each_call(tmp_path):
    """Mutate the file between calls; the second call must see the new state."""
    state = _write_state_csv(
        tmp_path / "s.csv",
        [_row("UCD-FIL-001", stage="intake")],
    )
    repo = CsvPipelineStateRepository(state)
    first = repo.get_state("UCD-FIL-001")
    assert first["stage"] == Stage.intake

    _write_state_csv(
        state,
        [_row("UCD-FIL-001", stage="verified", verify_ts="2026-05-12T10:00:00")],
    )
    second = repo.get_state("UCD-FIL-001")
    assert second["stage"] == Stage.verified


def test_csv_raw_segments_split_from_pipe(tmp_path):
    state = _write_state_csv(
        tmp_path / "s.csv",
        [_row("UCD-FIL-001", raw_segments="seg-a.mp4|seg-b.mp4|seg-c.mp4")],
    )
    repo = CsvPipelineStateRepository(state)
    s = repo.get_state("UCD-FIL-001")
    assert s["raw_segments"] == ["seg-a.mp4", "seg-b.mp4", "seg-c.mp4"]


# ----- raw_segments parsing surface (Brief #3.1 precondition) -----
#
# Brief #3.1 §4.6: My Cases pulls source segments from
# ``pipeline_state.raw_segments``. The list[str] surface is already
# established by ``PipelineStateRow``; these tests freeze that contract
# (single / multi happy paths + malformed-with-empty-segments rejection)
# so a future refactor of the row model can't quietly break the
# card-expansion source-segments display.


def test_pipeline_state_row_parses_single_segment():
    row = PipelineStateRow.from_csv_dict({
        "ucd_fil_id": "UCD-FIL-001",
        "raw_segments": "only-seg.mp4",
        "stage": "intake",
    })
    assert row.raw_segments == ["only-seg.mp4"]


def test_pipeline_state_row_parses_multi_segments():
    row = PipelineStateRow.from_csv_dict({
        "ucd_fil_id": "UCD-FIL-001",
        "raw_segments": "a.mp4|b.mp4|c.mp4",
        "stage": "verified",
    })
    assert row.raw_segments == ["a.mp4", "b.mp4", "c.mp4"]


def test_pipeline_state_row_rejects_empty_raw_segments():
    """Schema invariant: a state row must reference at least one
    segment. Empty cell → empty list → ``min_length=1`` rejects."""
    with pytest.raises(ValueError):
        PipelineStateRow.from_csv_dict({
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": "",
            "stage": "intake",
        })


def test_pipeline_state_row_rejects_malformed_segments_with_empty_element():
    """``"a.mp4||b.mp4"`` splits into ``["a.mp4", "", "b.mp4"]`` — the
    empty middle element trips ``_segments_pipe_safe``. Prevents a
    silently-broken segment list from reaching the surgeon UI."""
    with pytest.raises(ValueError):
        PipelineStateRow.from_csv_dict({
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": "a.mp4||b.mp4",
            "stage": "verified",
        })


# ----- InMemoryPipelineStateRepository -----


def test_inmem_list_for_case_ids():
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {"ucd_fil_id": "UCD-FIL-001", "stage": Stage.verified},
        "UCD-FIL-002": {"ucd_fil_id": "UCD-FIL-002", "stage": Stage.intake},
    })
    out = repo.list_for_case_ids(["UCD-FIL-001"])
    assert set(out.keys()) == {"UCD-FIL-001"}


def test_inmem_list_for_case_ids_empty():
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {"ucd_fil_id": "UCD-FIL-001", "stage": Stage.verified},
    })
    assert repo.list_for_case_ids([]) == {}


def test_inmem_get_state_returns_copy():
    """Mutation of the returned dict must not leak back into the repo."""
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {"ucd_fil_id": "UCD-FIL-001", "stage": Stage.intake},
    })
    s = repo.get_state("UCD-FIL-001")
    s["stage"] = Stage.verified
    again = repo.get_state("UCD-FIL-001")
    assert again["stage"] == Stage.intake


def test_inmem_get_state_unknown():
    repo = InMemoryPipelineStateRepository()
    assert repo.get_state("UCD-FIL-001") is None


def test_inmem_normalizes_string_stage():
    """For ergonomics — accepting raw ``"verified"`` strings makes inline
    test fixtures less noisy. Repo always surfaces a real Stage enum."""
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {"ucd_fil_id": "UCD-FIL-001", "stage": "verified"},
    })
    s = repo.get_state("UCD-FIL-001")
    assert s["stage"] == Stage.verified


def test_inmem_empty_by_default():
    repo = InMemoryPipelineStateRepository()
    assert repo.list_for_case_ids(["UCD-FIL-001"]) == {}
    assert repo.get_state("UCD-FIL-001") is None
