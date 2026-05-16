"""CaseManifestRepository — read access to ``case_manifest.csv``.

Distinct from :mod:`app.repos.cases`: that module owns the ownership /
authorization surface (``list_owned_by``, ``case_belongs_to``) plus the
submit-case write path. This module exposes the manifest row as a
typed dataclass (the actual 10-column Spec J schema) for callers that
need to read structured metadata for a single case — primarily the
My Cases tab's per-card expansion body.

Brief #3.1 §4.4: surface today is ``for_case_id`` only. Brief #4 will
extend with admin-facing reverse-lookup methods that bypass scope by
design (e.g., "which case owns this BDV filename?"). The schema
constants live in :mod:`pipeline.schemas` (single source of truth); this
module never invents column names.

Path resolution mirrors :mod:`app.repos.cases`: ``CASE_MANIFEST_PATH``
env var if set, else the NAS default. ``CsvCaseManifestRepository``
reads the live CSV fresh on every call (cache-free, snapshot-per-call).
``InMemoryCaseManifestRepository`` is the test fake.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.schemas import CASE_MANIFEST_COLUMNS


_DEFAULT_MANIFEST_PATH = Path("/mnt/nas/or-raw/case_manifest.csv")


def manifest_path() -> Path:
    env = os.environ.get("CASE_MANIFEST_PATH")
    if env:
        return Path(env)
    return _DEFAULT_MANIFEST_PATH


def _parse_additionals(raw: str | None) -> tuple[str, ...]:
    """Coerce the on-disk ``procedure_additional`` cell into a tuple. Empty
    / missing collapses to ``()`` silently; malformed JSON collapses to
    ``()`` rather than raising so a single bad row never takes the
    surgeon UI offline. Mirrors ``CsvCaseRepository`` read tolerance."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, str) and item)


@dataclass(frozen=True)
class CaseManifestRow:
    """Typed snapshot of one ``case_manifest.csv`` row.

    Fields mirror :data:`pipeline.schemas.CASE_MANIFEST_COLUMNS` exactly.
    ``procedure_additional`` is parsed from the on-disk JSON-encoded
    array into a tuple of non-empty strings; ``conversion_target`` is
    the empty string when the case is not a conversion (matches the
    on-disk convention)."""

    ucd_fil_id: str
    surgeon: str
    case_year: str
    or_room: str
    procedure_primary: str
    procedure_additional: tuple[str, ...]
    approach: str
    conversion_target: str
    indication: str
    notes: str

    @classmethod
    def from_row(cls, row: dict) -> "CaseManifestRow":
        return cls(
            ucd_fil_id=row.get("ucd_fil_id", ""),
            surgeon=row.get("surgeon", ""),
            case_year=row.get("case_year", ""),
            or_room=row.get("or_room", ""),
            procedure_primary=row.get("procedure_primary", ""),
            procedure_additional=_parse_additionals(
                row.get("procedure_additional")
            ),
            approach=row.get("approach", ""),
            conversion_target=row.get("conversion_target", "") or "",
            indication=row.get("indication", ""),
            notes=row.get("notes", "") or "",
        )


class CaseManifestRepository(Protocol):
    def for_case_id(self, case_id: str) -> CaseManifestRow | None: ...


class CsvCaseManifestRepository:
    """Reads ``case_manifest.csv`` (path from ``CASE_MANIFEST_PATH`` or
    :func:`manifest_path` default) fresh on every call. Missing file →
    ``None`` from ``for_case_id``; malformed rows that fail
    :func:`CaseManifestRow.from_row` parsing are skipped silently so a
    single bad row doesn't take the surgeon UI offline.

    Scope-agnostic: the repo doesn't enforce surgeon scope. Callers are
    responsible for verifying ownership before surfacing the result
    (the My Cases render path only looks up case_ids already in the
    surgeon's ``pipeline_state`` rows, so an out-of-scope id can't reach
    this repo through that path)."""

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

    def for_case_id(self, case_id: str) -> CaseManifestRow | None:
        for r in self._read_rows():
            if r.get("ucd_fil_id") == case_id:
                try:
                    return CaseManifestRow.from_row(r)
                except Exception:
                    return None
        return None


class InMemoryCaseManifestRepository:
    """Test fake. Initialize with an iterable of :class:`CaseManifestRow`
    instances (or dicts shaped like the on-disk row, which get parsed via
    :meth:`CaseManifestRow.from_row`)."""

    def __init__(
        self,
        rows: list[CaseManifestRow] | list[dict] | None = None,
    ):
        parsed: list[CaseManifestRow] = []
        for r in rows or []:
            if isinstance(r, CaseManifestRow):
                parsed.append(r)
            else:
                parsed.append(CaseManifestRow.from_row(r))
        self._rows: dict[str, CaseManifestRow] = {
            r.ucd_fil_id: r for r in parsed
        }

    def for_case_id(self, case_id: str) -> CaseManifestRow | None:
        return self._rows.get(case_id)


# Re-export for static checkers / tests that want to assert the full
# column list lines up with the repo's surface.
__all__ = (
    "CASE_MANIFEST_COLUMNS",
    "CaseManifestRepository",
    "CaseManifestRow",
    "CsvCaseManifestRepository",
    "InMemoryCaseManifestRepository",
    "manifest_path",
)
