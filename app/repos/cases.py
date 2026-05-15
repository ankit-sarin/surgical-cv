"""CaseRepository — data-access layer for case ownership and manifest reads.

v1 backing reads ``/mnt/nas/or-raw/case_manifest.csv`` directly on every call
(``CsvCaseRepository``). Stateless and cache-free — the CSV is small, NFS reads
are cheap, and snapshot-per-request consistency is fine for now. Future
migration to ``app.db`` touches only this module; the surfaces consuming it
(``SurgeonScope`` / ``AdminScope``) don't change.

Path resolution mirrors ``APP_DB_PATH`` / ``PIPELINE_PICKLIST_DIR``:
``CASE_MANIFEST_PATH`` env var if set, else the NAS default.

Initial method surface — grows as future specs need more (pipeline state,
metadata writes, etc.):

    list_owned_by(folder_slug) -> list[case_id]
    get_case(case_id) -> dict | None
    case_belongs_to(case_id, folder_slug) -> bool

``InMemoryCaseRepository`` is the test fake — dict-backed, no file I/O. Tests
inject it directly into ``SurgeonScope`` / ``AdminScope`` (unit) or use the
``CASE_MANIFEST_PATH`` env override to point ``CsvCaseRepository`` at a
tmpdir CSV (integration).
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable, Protocol

_DEFAULT_MANIFEST_PATH = Path("/mnt/nas/or-raw/case_manifest.csv")


def manifest_path() -> Path:
    env = os.environ.get("CASE_MANIFEST_PATH")
    if env:
        return Path(env)
    return _DEFAULT_MANIFEST_PATH


class CaseRepository(Protocol):
    def list_owned_by(self, folder_slug: str) -> list[str]: ...
    def get_case(self, case_id: str) -> dict | None: ...
    def case_belongs_to(self, case_id: str, folder_slug: str) -> bool: ...


class CsvCaseRepository:
    """Reads ``case_manifest.csv`` (path from ``CASE_MANIFEST_PATH`` or
    ``manifest_path()`` default) fresh on every call.

    The ``surgeon`` column in the manifest IS the ``folder_slug`` for
    ownership purposes (matches the pipeline's per-surgeon NAS folder names:
    sarin, miller, noren, flynn, kucejko). A missing file → empty results,
    not an exception; treats "no cases yet" the same as "no cases owned".
    """

    def __init__(self, path: Path | None = None):
        self._path_override = path

    def _path(self) -> Path:
        return self._path_override or manifest_path()

    def _read_rows(self) -> list[dict]:
        path = self._path()
        if not path.exists():
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    def list_owned_by(self, folder_slug: str) -> list[str]:
        return [
            r["ucd_fil_id"]
            for r in self._read_rows()
            if r.get("surgeon") == folder_slug
        ]

    def get_case(self, case_id: str) -> dict | None:
        for r in self._read_rows():
            if r.get("ucd_fil_id") == case_id:
                return dict(r)
        return None

    def case_belongs_to(self, case_id: str, folder_slug: str) -> bool:
        case = self.get_case(case_id)
        return case is not None and case.get("surgeon") == folder_slug


class InMemoryCaseRepository:
    """Test fake. Initialize with ``{case_id: row_dict}`` where ``row_dict``
    must contain at least a ``surgeon`` key (the folder_slug)."""

    def __init__(self, cases: dict[str, dict] | None = None):
        self._cases: dict[str, dict] = {
            cid: dict(row) for cid, row in (cases or {}).items()
        }

    def list_owned_by(self, folder_slug: str) -> list[str]:
        return [
            cid
            for cid, row in self._cases.items()
            if row.get("surgeon") == folder_slug
        ]

    def get_case(self, case_id: str) -> dict | None:
        row = self._cases.get(case_id)
        return dict(row) if row is not None else None

    def case_belongs_to(self, case_id: str, folder_slug: str) -> bool:
        row = self._cases.get(case_id)
        return row is not None and row.get("surgeon") == folder_slug
