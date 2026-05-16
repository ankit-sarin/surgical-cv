"""Worker-side PHI scan + in-place notes redaction (Brief #3.5a).

When the worker first picks up a marker, scan the manifest's
``notes`` column for PHI patterns. If anything found, rewrite the
notes in place via :func:`pipeline.phi_redact.scrub_text` and return
the structured scan result so Brief #3.5b (the rollup attention-item
emit) can act on it without re-scanning.

Clean-notes cases incur one CSV read and exit; no write, no
observable side effects.

Idempotent by construction: re-running on already-scrubbed text
returns ``{}`` because the canonical phi.py patterns don't match
the placeholder strings (``<NAME>``, ``<MRN>``, ``<DATE>``, etc.)
that ``scrub_text`` substitutes in.

Atomicity: the manifest rewrite goes through
:func:`pipeline.csv_io.CsvTable.transaction` — the project-wide
sanctioned write path. Crash mid-write leaves the original manifest
untouched.

V1 keeps no audit copy of the pre-scrubbed original notes. If a
future compliance review wants an audit trail, that's a small
follow-up spec (the natural shape: append the original to a
``notes_audit/<case_id>.txt`` on the NAS, never surfaced to the
surgeon UI).
"""

from __future__ import annotations

from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths
from pipeline.phi_redact import scrub_text
from pipeline.schemas import CASE_MANIFEST_COLUMNS, CaseManifestRow

from app.phi import scan_for_phi


def redact_case_notes(
    paths: NasPaths, case_id: str
) -> dict[str, int]:
    """Scan the manifest row's ``notes`` column for PHI. If any
    category matches, rewrite the column in place with the scrubbed
    version. Return the ``category → count`` mapping from
    :func:`scan_for_phi` so caller code (Brief #3.5b's emit path) can
    decide whether to record a rollup attention item.

    Behaviour:

      - Missing manifest row → returns ``{}``, no write, no exception.
      - Clean notes (or empty / None notes) → returns ``{}``, no write.
      - PHI found → atomically rewrites ``notes`` via
        ``scrub_text(notes)`` and returns the scan result.

    Idempotent: phi.py's patterns don't match scrub_text's
    placeholders, so re-running this on already-scrubbed text
    returns ``{}`` and skips the write.

    Atomicity: the rewrite goes through
    :func:`pipeline.csv_io.CsvTable.transaction`. The context manager
    only invokes the commit step when the transaction is dirty (i.e.,
    when ``tx.update`` was called) — clean-notes paths exit the
    ``with`` block with ``tx.dirty=False`` and the manifest stays
    untouched.
    """
    table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow,
    )
    with table.transaction() as tx:
        row = tx.find(case_id)
        if row is None:
            return {}
        notes = row.notes or ""
        scan = scan_for_phi(notes)
        if not scan:
            return {}
        scrubbed = scrub_text(notes)
        if scrubbed == notes:
            # Defensive: scan said PHI was present but scrub_text
            # produced an identical string. Should not happen with
            # phi.py / scrub_text sharing pipeline.phi_patterns —
            # but if it ever does, treat as a no-op write rather
            # than rewriting the column to its current value.
            return scan
        tx.update(case_id, notes=scrubbed)
    return scan
