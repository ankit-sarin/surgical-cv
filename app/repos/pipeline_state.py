"""PipelineStateRepository — read access to ``pipeline_state.csv``.

v1 backing reads ``/mnt/nas/or-raw/pipeline_state.csv`` directly on every
call (``CsvPipelineStateRepository``). Stateless and cache-free, mirroring
``CsvCaseRepository``: snapshot-per-request consistency is fine for the
small CSV the pipeline maintains.

Path resolution mirrors ``manifest_path()``: ``PIPELINE_STATE_PATH`` env
var if set, else the NAS default.

The primary call site is the surgeon "My Cases" tab — one
``list_for_case_ids`` call filters the global CSV down to the surgeon's
case set in memory.

``InMemoryPipelineStateRepository`` is the test fake, dict-backed.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Protocol

from pipeline.schemas import (
    PIPELINE_STATE_COLUMNS,
    PipelineStateRow,
    Stage,
)


_DEFAULT_STATE_PATH = Path("/mnt/nas/or-raw/pipeline_state.csv")


def state_path() -> Path:
    env = os.environ.get("PIPELINE_STATE_PATH")
    if env:
        return Path(env)
    return _DEFAULT_STATE_PATH


class PipelineStateRepository(Protocol):
    def list_for_case_ids(self, case_ids: list[str]) -> dict[str, dict]: ...
    def get_state(self, ucd_fil_id: str) -> dict | None: ...

    def list_all(self) -> list[dict]:
        """Brief #4: unscoped read for the admin Global Dashboard tab.
        Returns one dict per pipeline_state row, in CSV order. No role
        check inside the repo — the auth boundary lives at the admin
        mount point's role guard."""
        ...

    def case_id_for_source_file(self, filename: str) -> str | None:
        """Brief #4: reverse lookup. Returns the ``ucd_fil_id`` that has
        claimed ``filename`` in its ``raw_segments``, or ``None`` if no
        case has claimed it. Raises :class:`MultipleClaimsError` if more
        than one case claims the same source file — that's a pipeline
        state corruption the admin queue should investigate."""
        ...


def _row_to_dict(row: PipelineStateRow) -> dict:
    """Surface a state row at the repo boundary as a dict with Python-typed
    values: ``stage`` is a :class:`Stage` enum, ``raw_segments`` is
    ``list[str]``, all timestamp fields are strings (ISO 8601 or empty)."""
    return {
        "ucd_fil_id": row.ucd_fil_id,
        "raw_segments": list(row.raw_segments),
        "concat_filename": row.concat_filename,
        "deid_filename": row.deid_filename,
        "stage": row.stage,
        "intake_ts": row.intake_ts,
        "concat_ts": row.concat_ts,
        "deid_ts": row.deid_ts,
        "verify_ts": row.verify_ts,
        "verification_notes": row.verification_notes,
    }


class CsvPipelineStateRepository:
    """Reads ``pipeline_state.csv`` (path from ``PIPELINE_STATE_PATH`` or
    ``state_path()`` default) fresh on every call. Missing file → empty
    results, never an exception."""

    def __init__(self, path: Path | None = None):
        self._path_override = path

    def _path(self) -> Path:
        return self._path_override or state_path()

    def _read_rows(self) -> list[PipelineStateRow]:
        path = self._path()
        if not path.exists():
            return []
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            # Be tolerant of header drift here (don't raise) — the strict
            # validator lives in ``CsvTable`` for the write path. The repo
            # is read-only and a missing/extra column shouldn't take the
            # surgeon UI offline; the row model's ``from_csv_dict`` defaults
            # missing columns to empty.
            rows: list[PipelineStateRow] = []
            for raw in reader:
                rows.append(PipelineStateRow.from_csv_dict(raw))
            return rows

    def list_for_case_ids(self, case_ids: list[str]) -> dict[str, dict]:
        if not case_ids:
            return {}
        wanted = set(case_ids)
        return {
            r.ucd_fil_id: _row_to_dict(r)
            for r in self._read_rows()
            if r.ucd_fil_id in wanted
        }

    def get_state(self, ucd_fil_id: str) -> dict | None:
        for r in self._read_rows():
            if r.ucd_fil_id == ucd_fil_id:
                return _row_to_dict(r)
        return None

    def list_all(self) -> list[dict]:
        return [_row_to_dict(r) for r in self._read_rows()]

    def case_id_for_source_file(self, filename: str) -> str | None:
        from app.exceptions import MultipleClaimsError

        hits: list[str] = []
        for r in self._read_rows():
            if filename in r.raw_segments:
                hits.append(r.ucd_fil_id)
        if len(hits) > 1:
            raise MultipleClaimsError(filename, hits)
        return hits[0] if hits else None


class InMemoryPipelineStateRepository:
    """Test fake. Initialize with ``{case_id: state_dict}``. Each
    ``state_dict`` may use either the dict-shape returned by the CSV repo
    (``stage`` as :class:`Stage`) or string ``stage`` values; both are
    accepted for ergonomics, normalized on the way out."""

    def __init__(self, states: dict[str, dict] | None = None):
        self._states: dict[str, dict] = {}
        for cid, row in (states or {}).items():
            self._states[cid] = self._normalize(row)

    @staticmethod
    def _normalize(row: dict) -> dict:
        out = dict(row)
        if "stage" in out and isinstance(out["stage"], str):
            out["stage"] = Stage(out["stage"])
        if "raw_segments" in out:
            out["raw_segments"] = list(out["raw_segments"])
        return out

    def list_for_case_ids(self, case_ids: list[str]) -> dict[str, dict]:
        if not case_ids:
            return {}
        wanted = set(case_ids)
        return {
            cid: dict(self._states[cid])
            for cid in self._states
            if cid in wanted
        }

    def get_state(self, ucd_fil_id: str) -> dict | None:
        row = self._states.get(ucd_fil_id)
        return dict(row) if row is not None else None

    def list_all(self) -> list[dict]:
        return [dict(row) for row in self._states.values()]

    def case_id_for_source_file(self, filename: str) -> str | None:
        from app.exceptions import MultipleClaimsError

        hits = [
            cid for cid, row in self._states.items()
            if filename in (row.get("raw_segments") or [])
        ]
        if len(hits) > 1:
            raise MultipleClaimsError(filename, hits)
        return hits[0] if hits else None
