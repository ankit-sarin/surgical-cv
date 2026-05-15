"""Tests for ``app/worker/dispatch.py`` — per-marker stage progression
through a fake :class:`PipelineDriver` that synthetically advances
pipeline_state.csv. Production correctness relies on the real subprocess
driver in `SubprocessPipelineDriver`, smoke-covered separately."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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

from app.worker.dispatch import (
    DispatchOutcome,
    SubprocessResult,
    dispatch_marker,
    ensure_intake_row,
)
from app.worker.scan import Marker


@pytest.fixture
def paths(tmp_path) -> NasPaths:
    or_raw = tmp_path / "or-raw"
    or_raw.mkdir()
    p = NasPaths(
        root=tmp_path,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )
    _seed_manifest(p, "UCD-FIL-005", "sarin")
    return p


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


def _seed_manifest(paths: NasPaths, case_id: str, surgeon: str):
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
            notes="",
        ))


def _set_state_stage(paths: NasPaths, case_id: str, stage: Stage, **fields):
    """Helper for the fake driver — push the state row forward as a side
    effect of a "successful" pipeline subprocess call."""
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    with table.transaction() as tx:
        existing = tx.find(case_id)
        if existing is None:
            return
        tx.update(case_id, stage=stage, **fields)


@dataclass
class FakeDriver:
    """Records calls and produces scripted outcomes. Optionally invokes a
    side-effect callable per stage to advance the state CSV."""

    concat_returncode: int = 0
    deid_returncode: int = 0
    verify_returncode: int = 0
    concat_stderr: str = ""
    deid_stderr: str = ""
    verify_stderr: str = ""
    on_concat: Callable[[], None] | None = None
    on_deid: Callable[[], None] | None = None
    on_verify: Callable[[], None] | None = None
    calls: list[tuple] = field(default_factory=list)

    def concat(self, surgeon: str, case_id: str) -> SubprocessResult:
        self.calls.append(("concat", surgeon, case_id))
        if self.on_concat:
            self.on_concat()
        return SubprocessResult(
            returncode=self.concat_returncode, stdout="",
            stderr=self.concat_stderr,
        )

    def deid(self, surgeon: str, case_id: str) -> SubprocessResult:
        self.calls.append(("deid", surgeon, case_id))
        if self.on_deid:
            self.on_deid()
        return SubprocessResult(
            returncode=self.deid_returncode, stdout="",
            stderr=self.deid_stderr,
        )

    def verify(self, surgeon: str, case_id: str) -> SubprocessResult:
        self.calls.append(("verify", surgeon, case_id))
        if self.on_verify:
            self.on_verify()
        return SubprocessResult(
            returncode=self.verify_returncode, stdout="",
            stderr=self.verify_stderr,
        )


# ----- ensure_intake_row -----


def test_ensure_intake_row_creates_row(paths):
    ensure_intake_row(paths, "UCD-FIL-005", ["a.mp4", "b.mp4"])
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    rows = table.snapshot()
    assert len(rows) == 1
    assert rows[0].ucd_fil_id == "UCD-FIL-005"
    assert rows[0].stage == Stage.intake
    assert rows[0].raw_segments == ["a.mp4", "b.mp4"]


def test_ensure_intake_row_idempotent(paths):
    """Two calls for the same case_id leave a single row — second call is
    a no-op (operator-side state changes are preserved)."""
    ensure_intake_row(paths, "UCD-FIL-005", ["a.mp4"])
    _set_state_stage(paths, "UCD-FIL-005", Stage.concatenated,
                     concat_filename="sarin_20260515-080000.mp4",
                     concat_ts="2026-05-15T08:00:00")
    ensure_intake_row(paths, "UCD-FIL-005", ["a.mp4", "b.mp4"])
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    rows = table.snapshot()
    assert len(rows) == 1
    assert rows[0].stage == Stage.concatenated  # untouched


# ----- dispatch happy path -----


def _full_success_driver(paths: NasPaths, case_id: str) -> FakeDriver:
    return FakeDriver(
        on_concat=lambda: _set_state_stage(
            paths, case_id, Stage.concatenated,
            concat_filename="sarin_20260515-080000.mp4",
            concat_ts="2026-05-15T08:00:00",
        ),
        on_deid=lambda: _set_state_stage(
            paths, case_id, Stage.deidentified,
            deid_filename=f"{case_id}_video.mp4",
            deid_ts="2026-05-15T08:30:00",
        ),
        on_verify=lambda: _set_state_stage(
            paths, case_id, Stage.verified,
            verify_ts="2026-05-15T08:45:00",
            verification_notes="all checks passed",
        ),
    )


def test_dispatch_happy_path_through_all_three_stages(paths, marker):
    driver = _full_success_driver(paths, marker.ucd_fil_id)
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "success"
    assert driver.calls == [
        ("concat", "sarin", "UCD-FIL-005"),
        ("deid", "sarin", "UCD-FIL-005"),
        ("verify", "sarin", "UCD-FIL-005"),
    ]


def test_dispatch_creates_intake_row_if_missing(paths, marker):
    """Worker bootstraps the state CSV from the marker — manifest exists
    but state CSV is empty."""
    driver = _full_success_driver(paths, marker.ucd_fil_id)
    dispatch_marker(marker, paths, driver)
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    row = next(
        r for r in table.snapshot() if r.ucd_fil_id == marker.ucd_fil_id
    )
    assert row.stage == Stage.verified


# ----- dispatch — orphan -----


def test_dispatch_orphan_when_no_manifest_row(paths, marker, tmp_path):
    """Case not present in case_manifest.csv → orphan, no pipeline call."""
    # Wipe the manifest the fixture seeded.
    paths.manifest_csv.unlink()
    paths.manifest_csv.touch()
    driver = FakeDriver()
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "orphan"
    assert driver.calls == []


# ----- dispatch — hard failures at each stage -----


def test_dispatch_hard_fail_when_concat_returncode_nonzero(paths, marker):
    driver = FakeDriver(concat_returncode=1, concat_stderr="ffmpeg crash")
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "hard_fail"
    assert outcome.stage == "concat"
    assert outcome.returncode == 1
    assert "ffmpeg crash" in outcome.detail


def test_dispatch_hard_fail_when_concat_does_not_advance(paths, marker):
    """concat returncode 0 but state stayed at intake → hard fail."""
    driver = FakeDriver(concat_returncode=0)  # no on_concat side effect
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "hard_fail"
    assert outcome.stage == "concat"


def test_dispatch_hard_fail_when_deid_returncode_nonzero(paths, marker):
    driver = FakeDriver(
        deid_returncode=2, deid_stderr="ffmpeg deid failed",
        on_concat=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.concatenated,
            concat_filename="x.mp4", concat_ts="2026-05-15T08:00:00",
        ),
    )
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "hard_fail"
    assert outcome.stage == "deid"
    assert outcome.returncode == 2


def test_dispatch_hard_fail_when_deid_does_not_advance(paths, marker):
    driver = FakeDriver(
        deid_returncode=0,
        on_concat=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.concatenated,
            concat_filename="x.mp4", concat_ts="2026-05-15T08:00:00",
        ),
    )
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "hard_fail"
    assert outcome.stage == "deid"


# ----- dispatch — verify success / soft-fail -----


def test_dispatch_verify_soft_fail_when_state_lands_at_failed(paths, marker):
    """verify exits 1 (clean fail verdict) → state stage = failed → soft_fail."""
    driver = FakeDriver(
        verify_returncode=1,
        on_concat=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.concatenated,
            concat_filename="x.mp4", concat_ts="2026-05-15T08:00:00",
        ),
        on_deid=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.deidentified,
            deid_filename=f"{marker.ucd_fil_id}_video.mp4",
            deid_ts="2026-05-15T08:30:00",
        ),
        on_verify=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.failed,
            verify_ts="2026-05-15T08:45:00",
            verification_notes="audio leak detected",
        ),
    )
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "soft_fail"
    assert outcome.stage == "verify"
    assert "audio leak" in outcome.detail


def test_dispatch_verify_hard_fail_when_state_unexpected(paths, marker):
    """verify subprocess didn't update state to verified/failed — anomalous."""
    driver = FakeDriver(
        verify_returncode=0,
        on_concat=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.concatenated,
            concat_filename="x.mp4", concat_ts="2026-05-15T08:00:00",
        ),
        on_deid=lambda: _set_state_stage(
            paths, marker.ucd_fil_id, Stage.deidentified,
            deid_filename=f"{marker.ucd_fil_id}_video.mp4",
            deid_ts="2026-05-15T08:30:00",
        ),
        # on_verify is None — state stays at deidentified, not advanced.
    )
    outcome = dispatch_marker(marker, paths, driver)
    assert outcome.kind == "hard_fail"
    assert outcome.stage == "verify"


# ----- dispatch — truncation -----


def test_dispatch_truncates_long_stderr(paths, marker):
    driver = FakeDriver(
        concat_returncode=1,
        concat_stderr="x" * 5000,
    )
    outcome = dispatch_marker(marker, paths, driver)
    # First-1KB truncation per spec — the captured detail must be <= 1024.
    assert len(outcome.detail) <= 1024


# ----- SubprocessPipelineDriver instantiation -----


def test_subprocess_driver_instantiates_with_env_passthrough():
    """Construction smoke — production code path. Doesn't execute pipeline."""
    from app.worker.dispatch import SubprocessPipelineDriver

    d = SubprocessPipelineDriver(env={"PIPELINE_NAS_ROOT": "/tmp/x"})
    # The env is held privately; we just check construction succeeds.
    assert isinstance(d, SubprocessPipelineDriver)
