"""Tests for ``app/worker/main.py`` — argv parsing, lockfile gating,
iteration orchestration, end-to-end fixture-marker smoke."""

from __future__ import annotations

import json
import threading
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

from app.db.connection import connect
from app.worker.dispatch import SubprocessResult
from app.worker.lockfile import (
    WorkerAlreadyRunningError,
    default_lock_path,
    single_worker_lock,
)
from app.worker.main import main, run_iteration


@pytest.fixture
def nas(tmp_path) -> NasPaths:
    or_raw = tmp_path / "or-raw"
    or_raw.mkdir()
    return NasPaths(
        root=tmp_path,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )


def _seed_manifest(nas: NasPaths, case_id: str, surgeon: str):
    table = CsvTable(
        nas.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
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


def _drop_marker(nas: NasPaths, surgeon: str, case_id: str, payload=None):
    raw = nas.root / f"raw-{surgeon}"
    raw.mkdir(parents=True, exist_ok=True)
    marker = raw / f".ready-{case_id}.json"
    marker.write_text(json.dumps(payload or {
        "ucd_fil_id": case_id,
        "surgeon": surgeon,
        "submitted_at": "2026-05-15T08:00:00+00:00",
        "segments": ["capt0_20260515-080000.mp4"],
    }))
    return marker


def _set_state(nas: NasPaths, case_id: str, stage: Stage, **fields):
    table = CsvTable(
        nas.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    with table.transaction() as tx:
        if tx.find(case_id) is None:
            return
        tx.update(case_id, stage=stage, **fields)


class _AdvancingDriver:
    """Test double — advances the state CSV through every stage to verified."""

    def __init__(self, nas: NasPaths):
        self.nas = nas
        self.calls: list[tuple] = []

    def concat(self, surgeon):
        self.calls.append(("concat", surgeon))
        for row in CsvTable(
            self.nas.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
        ).snapshot():
            if row.stage == Stage.intake:
                _set_state(
                    self.nas, row.ucd_fil_id, Stage.concatenated,
                    concat_filename=f"{surgeon}_x.mp4",
                    concat_ts="2026-05-15T08:00:00",
                )
        return SubprocessResult(0, "", "")

    def deid(self, surgeon, case_id):
        self.calls.append(("deid", surgeon, case_id))
        _set_state(
            self.nas, case_id, Stage.deidentified,
            deid_filename=f"{case_id}_video.mp4",
            deid_ts="2026-05-15T08:30:00",
        )
        return SubprocessResult(0, "", "")

    def verify(self, surgeon, case_id):
        self.calls.append(("verify", surgeon, case_id))
        _set_state(
            self.nas, case_id, Stage.verified,
            verify_ts="2026-05-15T08:45:00",
            verification_notes="ok",
        )
        return SubprocessResult(0, "", "")


# ----- argv parsing -----


def test_main_requires_mode_flag(app_env, nas):
    with pytest.raises(SystemExit):
        main([])  # neither --once nor --daemon → argparse error


def test_main_rejects_both_once_and_daemon(app_env, nas):
    with pytest.raises(SystemExit):
        main(["--once", "--daemon"])


# ----- run_iteration on empty NAS -----


def test_run_iteration_empty_nas_is_quiet(app_env, nas):
    """No markers anywhere → no calls, no exceptions, all-zero counts."""
    driver = _AdvancingDriver(nas)
    counts = run_iteration(nas, driver)
    assert counts == {
        "success": 0, "soft_fail": 0, "hard_fail": 0,
        "orphan": 0, "malformed": 0,
    }
    assert driver.calls == []


# ----- end-to-end iteration smokes -----


def test_iteration_processes_clean_marker_end_to_end(app_env, nas):
    """Drop a marker for a manifest-present case → run iteration → state
    CSV at verified, marker in .processed/, no attention_items."""
    _seed_manifest(nas, "UCD-FIL-005", "sarin")
    marker = _drop_marker(nas, "sarin", "UCD-FIL-005")
    from app.worker.failures import ensure_system_worker_user
    ensure_system_worker_user()

    driver = _AdvancingDriver(nas)
    counts = run_iteration(nas, driver)

    assert counts["success"] == 1
    assert not marker.exists()
    assert (marker.parent / ".processed" / marker.name).exists()

    state = next(
        r for r in CsvTable(
            nas.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
        ).snapshot() if r.ucd_fil_id == "UCD-FIL-005"
    )
    assert state.stage == Stage.verified
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM attention_items"
        ).fetchone()[0]
    assert n == 0


def test_iteration_failure_smoke_orphan_marker(app_env, nas):
    """Marker for a case NOT in the manifest → orphan → marker in .failed/,
    one attention_items row."""
    marker = _drop_marker(nas, "sarin", "UCD-FIL-999")
    from app.worker.failures import ensure_system_worker_user
    ensure_system_worker_user()

    driver = _AdvancingDriver(nas)
    counts = run_iteration(nas, driver)

    assert counts["orphan"] == 1
    assert (marker.parent / ".failed" / marker.name).exists()
    with connect() as conn:
        rows = list(conn.execute("SELECT * FROM attention_items"))
    assert len(rows) == 1
    assert rows[0]["type"] == "orphan_marker"
    # Pipeline subprocess was never invoked.
    assert driver.calls == []


def test_iteration_malformed_marker_quarantined(app_env, nas):
    """Marker file exists but JSON is garbage → .malformed/ + attention item."""
    raw = nas.root / "raw-sarin"
    raw.mkdir()
    bad = raw / ".ready-UCD-FIL-005.json"
    bad.write_text("not_json{{{")
    from app.worker.failures import ensure_system_worker_user
    ensure_system_worker_user()

    driver = _AdvancingDriver(nas)
    counts = run_iteration(nas, driver)

    assert counts["malformed"] == 1
    assert (raw / ".malformed" / bad.name).exists()
    assert driver.calls == []


def test_iteration_processes_multiple_markers_in_fifo(app_env, nas):
    """Two markers, oldest mtime first. Both should succeed and end up
    in their respective .processed/."""
    import os
    import time

    _seed_manifest(nas, "UCD-FIL-005", "sarin")
    _seed_manifest(nas, "UCD-FIL-006", "sarin")
    m1 = _drop_marker(nas, "sarin", "UCD-FIL-005")
    old_t = time.time() - 5
    os.utime(m1, (old_t, old_t))
    m2 = _drop_marker(nas, "sarin", "UCD-FIL-006",
                      payload={
                          "ucd_fil_id": "UCD-FIL-006",
                          "surgeon": "sarin",
                          "submitted_at": "2026-05-15T09:00:00+00:00",
                          "segments": ["capt0_20260515-090000.mp4"],
                      })
    from app.worker.failures import ensure_system_worker_user
    ensure_system_worker_user()

    driver = _AdvancingDriver(nas)
    counts = run_iteration(nas, driver)
    assert counts["success"] == 2


# ----- lockfile enforcement -----


def test_main_refuses_to_start_when_lock_held(app_env, nas, monkeypatch):
    """A second --once invocation while the lock is held → exit 2."""
    driver = _AdvancingDriver(nas)
    lock = default_lock_path(nas.root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with single_worker_lock(lock):
        rc = main(["--once"], paths=nas, driver=driver)
    assert rc == 2


def test_main_once_runs_when_lock_free(app_env, nas):
    driver = _AdvancingDriver(nas)
    rc = main(["--once"], paths=nas, driver=driver)
    assert rc == 0


# ----- daemon mode -----


def test_main_daemon_iterates_then_keyboard_interrupt(app_env, nas):
    """Daemon mode runs at least one iteration, then a fake sleep that
    raises KeyboardInterrupt simulates the operator pressing Ctrl-C."""
    _seed_manifest(nas, "UCD-FIL-005", "sarin")
    _drop_marker(nas, "sarin", "UCD-FIL-005")
    driver = _AdvancingDriver(nas)

    sleep_calls = []

    def fake_sleep(secs):
        sleep_calls.append(secs)
        # First sleep → interrupt to exit the daemon loop.
        raise KeyboardInterrupt

    rc = main(
        ["--daemon", "--interval", "60"],
        paths=nas, driver=driver, sleep_fn=fake_sleep,
    )
    assert rc == 0
    # Iteration ran before the interrupt.
    assert driver.calls != []
    assert sleep_calls == [60]


def test_main_daemon_default_interval(app_env, nas):
    driver = _AdvancingDriver(nas)

    def fake_sleep(secs):
        # Capture and bail.
        fake_sleep.captured = secs
        raise KeyboardInterrupt

    fake_sleep.captured = None
    main(["--daemon"], paths=nas, driver=driver, sleep_fn=fake_sleep)
    assert fake_sleep.captured == 60  # spec default
