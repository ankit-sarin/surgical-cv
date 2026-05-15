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
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

from app.repos.segments import raw_root


_log = logging.getLogger(__name__)

# F-011: surgeon-facing message for any infrastructure failure during
# submit_case. Generic by design — no path, no errno, no exception detail.
# Operators get the full context via the journalctl-captured logger above;
# the surgeon sees an actionable instruction instead of a stack-trace shape.
_SUBMIT_GENERIC_MSG = (
    "Submission could not be saved. Please contact your coordinator."
)

_DEFAULT_MANIFEST_PATH = Path("/mnt/nas/or-raw/case_manifest.csv")


_CASE_ID_PATTERN = re.compile(r"^UCD-FIL-(\d{3,})$")


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of a successful submit_case call."""
    ucd_fil_id: str
    submitted_at: str  # ISO 8601 UTC


class SubmitError(Exception):
    """Submit failure that the surgeon UI should surface as a soft error
    (manifest unreachable, marker write failed, allocation race, etc.)."""


class RepoIntegrityError(Exception):
    """Repo-layer invariant violation. Distinct from ``SubmitError`` (which
    is for surgeon-facing infrastructure soft-failures) — this is a
    programming / architectural breach (e.g., a caller passed a surgeon
    string that doesn't match the authenticated identity). Should fail loud,
    never collapse into a polite UI error.

    F-013: raised by ``submit_case`` when the caller-supplied
    ``partial_row['surgeon']`` does not match ``expected_surgeon``."""


def _next_ucd_fil_id(existing_ids: Iterable[str]) -> str:
    """Allocate the next case ID as max-numeric-suffix + 1, zero-padded to
    three digits. Tolerates gaps (001/002/005 → 006). Non-matching ids
    are ignored — they don't participate in the allocation."""
    max_n = 0
    for case_id in existing_ids:
        m = _CASE_ID_PATTERN.match(case_id)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return f"UCD-FIL-{max_n + 1:03d}"


def _write_ready_marker(
    raw_video_root: Path,
    surgeon: str,
    ucd_fil_id: str,
    submitted_at: str,
    segment_filenames: list[str],
) -> Path:
    """Drop a ``.ready-<ucd_fil_id>.json`` marker in the surgeon's raw-video
    folder. The dot-prefix hides it from BDV / Citrix browsing. Written via
    temp-file + atomic rename so the future Q3 worker never observes a
    half-written marker."""
    raw_dir = raw_video_root / f"raw-{surgeon}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    marker_path = raw_dir / f".ready-{ucd_fil_id}.json"
    payload = {
        "ucd_fil_id": ucd_fil_id,
        "surgeon": surgeon,
        "submitted_at": submitted_at,
        "segments": list(segment_filenames),
    }
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".ready-{ucd_fil_id}.",
        suffix=".tmp",
        dir=str(raw_dir),
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(marker_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return marker_path


def _parse_additionals(raw: str | None) -> list[str]:
    """Coerce the on-disk procedure_additional cell into a list. Empty /
    missing collapses to [] silently; malformed JSON is logged as [] rather
    than raising so the read path stays tolerant (the metadata CLI is the
    write-side validator)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item]


def manifest_path() -> Path:
    env = os.environ.get("CASE_MANIFEST_PATH")
    if env:
        return Path(env)
    return _DEFAULT_MANIFEST_PATH


class CaseRepository(Protocol):
    def list_owned_by(self, folder_slug: str) -> list[str]: ...
    def get_case(self, case_id: str) -> dict | None: ...
    def case_belongs_to(self, case_id: str, folder_slug: str) -> bool: ...
    def submit_case(
        self,
        partial_row: dict,
        segment_filenames: list[str],
        *,
        expected_surgeon: str,
    ) -> SubmitResult: ...


class CsvCaseRepository:
    """Reads ``case_manifest.csv`` (path from ``CASE_MANIFEST_PATH`` or
    ``manifest_path()`` default) fresh on every call.

    The ``surgeon`` column in the manifest IS the ``folder_slug`` for
    ownership purposes (matches the pipeline's per-surgeon NAS folder names:
    sarin, miller, noren, flynn, kucejko). A missing file → empty results,
    not an exception; treats "no cases yet" the same as "no cases owned".

    ``submit_case`` writes through the project-wide ``CsvTable.transaction``
    locked-rewrite pattern (fcntl.flock on a sibling ``.lock``, atomic
    tempfile + os.replace). Same correctness guarantee as the spec's
    "flock on case_manifest.csv" wording — the project's standing convention
    is the sibling-lock variant.
    """

    def __init__(
        self,
        path: Path | None = None,
        raw_video_root: Path | None = None,
    ):
        self._path_override = path
        self._raw_root_override = raw_video_root

    def _path(self) -> Path:
        return self._path_override or manifest_path()

    def _raw_root(self) -> Path:
        return self._raw_root_override or raw_root()

    def _read_rows(self) -> list[dict]:
        path = self._path()
        if not path.exists():
            return []
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        # Surface procedure_additional as list[str] rather than leaking the
        # on-disk JSON-string encoding to callers (Spec K's submit handler,
        # surgeon_app rendering, etc.).
        for r in rows:
            if "procedure_additional" in r:
                r["procedure_additional"] = _parse_additionals(
                    r.get("procedure_additional")
                )
        return rows

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

    def submit_case(
        self,
        partial_row: dict,
        segment_filenames: list[str],
        *,
        expected_surgeon: str,
    ) -> SubmitResult:
        """Allocate a new ``ucd_fil_id`` under flock, append the row, then
        drop the ready marker. Returns ``SubmitResult`` on success;
        raises ``SubmitError`` on any infrastructure failure.

        F-013: ``expected_surgeon`` MUST equal ``partial_row['surgeon']``.
        Mismatch raises ``RepoIntegrityError`` before any I/O — fail fast,
        no manifest read, no marker write, no lock acquired. The check
        guards against a future caller threading a less-trusted surgeon
        string (admin cross-surgeon path, Action Required resolve-on-behalf,
        a CLI helper); today's only caller already passes the authenticated
        ``scope.folder_slug`` so the assertion is a defense-in-depth gate."""
        if partial_row.get("surgeon") != expected_surgeon:
            raise RepoIntegrityError(
                f"submit_case surgeon mismatch: "
                f"expected={expected_surgeon!r}, "
                f"partial_row={partial_row.get('surgeon')!r}"
            )

        from pipeline.csv_io import CsvTable
        from pipeline.schemas import CASE_MANIFEST_COLUMNS, CaseManifestRow

        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        table = CsvTable(path, CASE_MANIFEST_COLUMNS, CaseManifestRow)
        submitted_at = datetime.now(timezone.utc).isoformat()

        new_id: str | None = None
        try:
            with table.transaction() as tx:
                existing_ids = [r.ucd_fil_id for r in tx.read_all()]
                new_id = _next_ucd_fil_id(existing_ids)
                row = CaseManifestRow(
                    ucd_fil_id=new_id,
                    surgeon=partial_row["surgeon"],
                    case_year=partial_row["case_year"],
                    or_room=partial_row["or_room"],
                    procedure_primary=partial_row["procedure_primary"],
                    procedure_additional=list(
                        partial_row.get("procedure_additional") or []
                    ),
                    approach=partial_row["approach"],
                    conversion_target=partial_row.get("conversion_target") or "",
                    indication=partial_row["indication"],
                    notes=partial_row.get("notes") or "",
                )
                tx.append(row)
        except Exception as e:
            # F-011: full context (path, surgeon, partial new_id, exception
            # type) lands in the systemd journal via the logger; the surgeon
            # sees only the curated generic message. ``exc_info=True`` chains
            # the original traceback so operators can still walk it. The
            # ``from e`` clause on the raise preserves __cause__ for any
            # downstream debugger that introspects the exception chain.
            _log.exception(
                "submit_case: manifest write failed",
                extra={
                    "manifest_path": str(path),
                    "surgeon": expected_surgeon,
                    "ucd_fil_id_attempted": new_id,
                    "error_type": type(e).__name__,
                },
            )
            raise SubmitError(_SUBMIT_GENERIC_MSG) from e

        try:
            _write_ready_marker(
                self._raw_root(),
                partial_row["surgeon"],
                new_id,
                submitted_at,
                segment_filenames,
            )
        except Exception as e:
            # Manifest already committed — surface the partial-failure so
            # the surgeon can re-trigger marker writing manually if needed.
            raise SubmitError(
                f"manifest committed as {new_id} but ready marker failed: {e}"
            ) from e

        return SubmitResult(ucd_fil_id=new_id, submitted_at=submitted_at)


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

    def submit_case(
        self,
        partial_row: dict,
        segment_filenames: list[str],
        *,
        expected_surgeon: str,
    ) -> SubmitResult:
        """Simulated atomic allocation for tests — same ID-allocation
        semantics as the CSV-backed repo, no marker file written. Mirrors
        the F-013 surgeon-mismatch guard so tests that exercise the in-memory
        repo see the same fail-fast behavior as production."""
        if partial_row.get("surgeon") != expected_surgeon:
            raise RepoIntegrityError(
                f"submit_case surgeon mismatch: "
                f"expected={expected_surgeon!r}, "
                f"partial_row={partial_row.get('surgeon')!r}"
            )
        new_id = _next_ucd_fil_id(self._cases.keys())
        submitted_at = datetime.now(timezone.utc).isoformat()
        self._cases[new_id] = {
            "ucd_fil_id": new_id,
            "surgeon": partial_row["surgeon"],
            "case_year": partial_row["case_year"],
            "or_room": partial_row["or_room"],
            "procedure_primary": partial_row["procedure_primary"],
            "procedure_additional": list(
                partial_row.get("procedure_additional") or []
            ),
            "approach": partial_row["approach"],
            "conversion_target": partial_row.get("conversion_target") or "",
            "indication": partial_row["indication"],
            "notes": partial_row.get("notes") or "",
            "segments": list(segment_filenames),
            "submitted_at": submitted_at,
        }
        return SubmitResult(ucd_fil_id=new_id, submitted_at=submitted_at)
