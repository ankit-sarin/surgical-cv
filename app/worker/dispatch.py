"""Per-marker pipeline-stage driver.

Drives a single case through ``concat → deid → verify`` via the existing
pipeline CLI subcommands. Each stage's progress is observed through
``pipeline_state.csv`` (the worker doesn't read pipeline subprocess output
to decide success — exit code + post-stage CSV state are the source of truth).

The ``PipelineDriver`` protocol exists so tests can swap a fake that
synthetically advances the state CSV without spawning real subprocesses.
The default ``SubprocessPipelineDriver`` shells out to ``python -m pipeline``."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths
from pipeline.schemas import (
    PIPELINE_STATE_COLUMNS,
    PipelineStateRow,
    Stage,
)

from app.worker.scan import Marker

# First 1KB of stderr is what attention_items.details captures — enough for
# triage without blowing the SQLite row.
_STDERR_TRUNC = 1024


@dataclass(frozen=True)
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str


class PipelineDriver(Protocol):
    def concat(self, surgeon: str) -> SubprocessResult: ...
    def deid(self, surgeon: str, case_id: str) -> SubprocessResult: ...
    def verify(self, surgeon: str, case_id: str) -> SubprocessResult: ...


class SubprocessPipelineDriver:
    """Real implementation — shells out to ``python -m pipeline``. The env
    is inherited so ``PIPELINE_NAS_ROOT`` / ``PIPELINE_PICKLIST_DIR`` etc.
    flow through to the subcommand."""

    def __init__(self, env: dict | None = None):
        self._env = env if env is not None else os.environ.copy()

    def concat(self, surgeon: str) -> SubprocessResult:
        return self._run(["concat", "--surgeon", surgeon])

    def deid(self, surgeon: str, case_id: str) -> SubprocessResult:
        return self._run(["deid", "--surgeon", surgeon, "--case", case_id])

    def verify(self, surgeon: str, case_id: str) -> SubprocessResult:
        return self._run(["verify", "--surgeon", surgeon, "--case", case_id])

    def _run(self, argv: list[str]) -> SubprocessResult:
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline", *argv],
            capture_output=True,
            text=True,
            env=self._env,
        )
        return SubprocessResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of driving one marker through the pipeline.

    ``kind`` ∈ {"success", "soft_fail", "hard_fail", "orphan"}:
      - success:   reached stage=verified.
      - soft_fail: verify reported a clean-fail verdict (stage=failed via verify).
                   Case is terminal-but-flagged; the marker archives to .processed/.
      - hard_fail: pipeline subprocess returned non-zero before verify, OR a
                   stage transition didn't land in the expected post-state.
      - orphan:    no row in case_manifest.csv for this ucd_fil_id.
    """

    kind: str
    stage: str = ""
    returncode: int = 0
    detail: str = ""


def _truncate(s: str) -> str:
    return s[:_STDERR_TRUNC] if s else ""


def _case_in_manifest(paths: NasPaths, case_id: str) -> bool:
    from pipeline.schemas import CASE_MANIFEST_COLUMNS, CaseManifestRow

    table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    for row in table.snapshot():
        if row.ucd_fil_id == case_id:
            return True
    return False


def _get_state_row(paths: NasPaths, case_id: str) -> PipelineStateRow | None:
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    for row in table.snapshot():
        if row.ucd_fil_id == case_id:
            return row
    return None


def ensure_intake_row(
    paths: NasPaths, case_id: str, segments: list[str]
) -> None:
    """Idempotent bootstrap. If the case has no pipeline_state row, insert
    at stage=intake with the marker's segments. If a row already exists,
    leave it alone (operator may have manually advanced or retried it)."""
    table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )
    with table.transaction() as tx:
        if tx.find(case_id) is not None:
            return
        row = PipelineStateRow(
            ucd_fil_id=case_id,
            raw_segments=segments,
            concat_filename="",
            deid_filename="",
            stage=Stage.intake,
            concat_ts="",
            deid_ts="",
            verify_ts="",
            verification_notes="",
        )
        tx.append(row)


def dispatch_marker(
    marker: Marker,
    paths: NasPaths,
    driver: PipelineDriver,
) -> DispatchOutcome:
    """Drive ``marker``'s case from intake → verified (or failure).

    Pre-conditions checked:
      - The case must exist in ``case_manifest.csv``; missing manifest row →
        ``orphan`` outcome (no pipeline subprocess invoked).

    Stage progression:
      1. Ensure intake-stage state row exists (from marker segments).
      2. ``concat --surgeon`` (batch).      Expect post-stage = concatenated.
      3. ``deid --surgeon --case``.        Expect post-stage = deidentified.
      4. ``verify --surgeon --case``.      Expect post-stage = verified
                                            (success) or failed (soft_fail).
    """
    if not _case_in_manifest(paths, marker.ucd_fil_id):
        return DispatchOutcome(
            kind="orphan",
            detail=f"case {marker.ucd_fil_id} not present in case_manifest.csv",
        )

    ensure_intake_row(paths, marker.ucd_fil_id, marker.segments)

    # Stage 1: concat (batch over surgeon — idempotent for already-concatenated rows)
    rc = driver.concat(marker.surgeon)
    if rc.returncode != 0:
        return DispatchOutcome(
            kind="hard_fail",
            stage="concat",
            returncode=rc.returncode,
            detail=_truncate(rc.stderr),
        )
    state = _get_state_row(paths, marker.ucd_fil_id)
    if state is None or state.stage == Stage.intake:
        return DispatchOutcome(
            kind="hard_fail",
            stage="concat",
            returncode=rc.returncode,
            detail=(
                f"case did not advance past intake after concat; "
                f"stage={state.stage.value if state else 'missing'}"
            ),
        )
    if state.stage == Stage.failed:
        return DispatchOutcome(
            kind="hard_fail",
            stage="concat",
            returncode=rc.returncode,
            detail=f"concat marked the case failed: {state.verification_notes}",
        )

    # Stage 2: deid (per-case)
    rc = driver.deid(marker.surgeon, marker.ucd_fil_id)
    if rc.returncode != 0:
        return DispatchOutcome(
            kind="hard_fail",
            stage="deid",
            returncode=rc.returncode,
            detail=_truncate(rc.stderr),
        )
    state = _get_state_row(paths, marker.ucd_fil_id)
    if state is None or state.stage != Stage.deidentified:
        return DispatchOutcome(
            kind="hard_fail",
            stage="deid",
            returncode=rc.returncode,
            detail=(
                f"case did not advance to deidentified after deid; "
                f"stage={state.stage.value if state else 'missing'}"
            ),
        )

    # Stage 3: verify (per-case) — verify returncode is informational only;
    # the state CSV's stage is the source of truth (verify can produce
    # stage=verified or stage=failed depending on the diagnostician verdict).
    rc = driver.verify(marker.surgeon, marker.ucd_fil_id)
    state = _get_state_row(paths, marker.ucd_fil_id)
    if state is None:
        return DispatchOutcome(
            kind="hard_fail",
            stage="verify",
            returncode=rc.returncode,
            detail="state row vanished after verify",
        )
    if state.stage == Stage.verified:
        return DispatchOutcome(
            kind="success", stage="verify", returncode=rc.returncode,
        )
    if state.stage == Stage.failed:
        # Diagnostician returned a clean fail verdict — terminal-but-flagged.
        return DispatchOutcome(
            kind="soft_fail",
            stage="verify",
            returncode=rc.returncode,
            detail=state.verification_notes or _truncate(rc.stderr),
        )
    return DispatchOutcome(
        kind="hard_fail",
        stage="verify",
        returncode=rc.returncode,
        detail=(
            f"unexpected stage after verify: {state.stage.value} "
            f"(expected verified or failed)"
        ),
    )
