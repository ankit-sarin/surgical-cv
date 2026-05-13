"""metadata subcommand — read, dry-run preview, and commit edits to
case_manifest.csv.

Three dispatch paths from run(args):
  --show (or no flags)        read-only render of all 8 manifest fields
                              plus a one-line pipeline-stage summary.
                              Silent on the audit log.
  --edit FIELD VALUE          dry-run: validates and previews "DRY RUN — ..."
                              with Before/After. Silent on the audit log.
  --edit FIELD VALUE --confirm commit path: opens CsvTable.transaction(),
                              captures `before` from the locked snapshot
                              (NOT the pre-transaction snapshot), writes
                              one cell, and logs exactly one audit entry
                              with failure_kind ∈ {format, not_found,
                              validation, infra, exception} on failure or
                              outcome="success" on commit. Notes-field
                              commits also emit a soft nudge to stderr.

The five failure_kind values are metadata-specific and live alongside
verify's existing discriminators in the audit log; audit.py's schema is
unchanged.
"""

import json
import os
import re
import sys
import traceback
from argparse import Namespace
from pathlib import Path

from pipeline.audit import log_audit
from pipeline.csv_io import CsvTable, RowNotFoundError
from pipeline.paths import NasPaths, resolve_paths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)


_CASE_RE = re.compile(r"^UCD-FIL-\d{3}$")
# Mirrors CaseManifestRow.case_year Field(pattern=r"^\d{4}$") in pipeline/schemas.py.
_YEAR_RE = re.compile(r"^\d{4}$")

_LABEL_WIDTH = 16
_HRULE = "─" * 14
_FAILED_NOTES_TRUNC = 80

_PICKLIST_FIELDS: dict[str, str] = {
    "procedure_name": "procedures",
    "approach": "approaches",
    "indication": "indications",
}

_NOTES_NUDGE = (
    "note: free-text field; PHI screening happens downstream via surgeon-audit."
)


class _InfraError(Exception):
    """Vocab-load failure or other infrastructure-level problem. Exit 2."""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _vocab_dir() -> Path:
    env = os.environ.get("PIPELINE_VOCAB_DIR")
    return Path(env) if env else _project_root() / "bench" / "vocabularies"


def _load_vocab(name: str) -> list[str]:
    path = _vocab_dir() / f"{name}.json"
    if not path.exists():
        raise _InfraError(f"vocab file missing: {path}")
    try:
        text = path.read_text()
    except OSError as e:
        raise _InfraError(f"vocab file unreadable at {path}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise _InfraError(f"vocab file malformed at {path}: {e}") from e
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise _InfraError(
            f"vocab file at {path} must be a JSON array of strings"
        )
    return data


def run(args: Namespace, paths: NasPaths | None = None) -> int:
    """Dispatch metadata subcommand by argparse flags.

    Routes --edit + --confirm to the commit path, --edit alone to the
    dry-run preview, and the no-flag / --show form to the read-only
    render. See module docstring for the audit-log policy on each path.
    """
    if args.edit is not None:
        if args.confirm:
            return _commit(args, paths)
        return _dry_run(args, paths)
    return _show(args, paths)


# ----- shared helpers -----


def _audit_args(args: Namespace) -> dict:
    field, value = args.edit
    return {"edit_field": field, "edit_value": value, "confirm": True}


def _validate_field(field: str, value: str) -> str | None:
    """Return None on valid input, or a human-readable failure reason.
    May raise _InfraError on vocab-load problems (caller maps that to exit 2).
    """
    if field == "case_year":
        if not _YEAR_RE.match(value):
            return f"expected 4-digit year, got {value!r}"
        vocab = _load_vocab("case_years")
        if value not in vocab:
            return (
                f"year {value!r} not in case_years allowlist "
                f"({len(vocab)} allowed: {vocab[0]}-{vocab[-1]})"
            )
        return None
    if field == "or_room":
        if value.strip() == "":
            return "or_room must be non-empty"
        return None
    if field == "notes":
        return None
    if field in _PICKLIST_FIELDS:
        vocab_name = _PICKLIST_FIELDS[field]
        vocab = _load_vocab(vocab_name)
        if value not in vocab:
            return (
                f"{value!r} not in {vocab_name} vocabulary "
                f"({len(vocab)} allowed values)"
            )
        return None
    return f"unknown field {field!r}"


def _render_edit_block(header: str, case_id: str, field: str, before: str, after: str) -> str:
    before_disp = before if before != "" else "(empty)"
    after_disp = after if after != "" else "(empty)"
    return "\n".join(
        [
            header,
            f"{'Case:':<{_LABEL_WIDTH}}{case_id}",
            f"{'Field:':<{_LABEL_WIDTH}}{field}",
            f"{'Before:':<{_LABEL_WIDTH}}{before_disp}",
            f"{'After:':<{_LABEL_WIDTH}}{after_disp}",
        ]
    )


# ----- --edit FIELD VALUE (dry-run, no --confirm) -----


def _dry_run(args: Namespace, paths: NasPaths | None) -> int:
    case_id = args.ucd_fil_id
    if not _CASE_RE.match(case_id):
        print(f"error: invalid case ID format: {case_id}", file=sys.stderr)
        return 1

    if paths is None:
        paths = resolve_paths()

    manifest_by_id = {
        m.ucd_fil_id: m
        for m in CsvTable(
            paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
        ).snapshot()
    }
    manifest_row = manifest_by_id.get(case_id)
    if manifest_row is None:
        print(f"error: case not found in manifest: {case_id}", file=sys.stderr)
        return 1

    field, value = args.edit
    before = getattr(manifest_row, field)

    try:
        reason = _validate_field(field, value)
    except _InfraError as e:
        print(f"infrastructure error: {e}", file=sys.stderr)
        return 2

    if reason is not None:
        print(f"validation error: {field}: {reason}", file=sys.stderr)
        return 1

    print(
        _render_edit_block(
            "DRY RUN — no changes written. Add --confirm to commit.",
            case_id,
            field,
            before,
            value,
        )
    )
    return 0


# ----- --edit FIELD VALUE --confirm (commit path) -----


def _commit(args: Namespace, paths: NasPaths | None) -> int:
    case_id = args.ucd_fil_id
    field, new_value = args.edit
    audit_args = _audit_args(args)

    if paths is None:
        paths = resolve_paths()

    # Step 1 — format check (audit on failure, case= omitted because the arg
    # isn't a real case ID).
    if not _CASE_RE.match(case_id):
        log_audit(
            paths.audit_log,
            "metadata",
            audit_args,
            "failure",
            details={
                "failure_kind": "format",
                "case_arg": case_id,
                "error": "case ID format invalid",
            },
        )
        print(f"error: invalid case ID format: {case_id}", file=sys.stderr)
        return 1

    # Step 2 — pre-snapshot existence check. The pre-snapshot's row contents
    # are informational only; the audit's `before` field comes from the
    # locked transaction snapshot in step 5.
    manifest_table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    manifest_by_id = {m.ucd_fil_id: m for m in manifest_table.snapshot()}
    if case_id not in manifest_by_id:
        log_audit(
            paths.audit_log,
            "metadata",
            audit_args,
            "failure",
            case=case_id,
            details={"failure_kind": "not_found"},
        )
        print(f"error: case not found in manifest: {case_id}", file=sys.stderr)
        return 1

    # Steps 3 + 4 — vocab load and value validation.
    try:
        reason = _validate_field(field, new_value)
    except _InfraError as e:
        log_audit(
            paths.audit_log,
            "metadata",
            audit_args,
            "failure",
            case=case_id,
            details={"failure_kind": "infra", "detail": str(e)},
        )
        print(f"infrastructure error: {e}", file=sys.stderr)
        return 2

    if reason is not None:
        log_audit(
            paths.audit_log,
            "metadata",
            audit_args,
            "failure",
            case=case_id,
            details={
                "failure_kind": "validation",
                "field": field,
                "value": new_value,
                "reason": reason,
            },
        )
        print(f"validation error: {field}: {reason}", file=sys.stderr)
        return 1

    # Step 5 — commit. The locked snapshot is the source of truth for
    # `before`. CsvTable.transaction() commits via atomic tempfile rename
    # only if no exception fires inside the `with` block (verified by
    # reading csv_io.py:_commit and the context manager).
    before_value: str | None = None
    try:
        with manifest_table.transaction() as tx:
            locked_row = tx.find(case_id)
            if locked_row is None:
                # Race: case vanished between pre-snapshot and lock acquire.
                # Treated as exception so the transaction does not commit.
                raise RowNotFoundError(case_id)
            before_value = getattr(locked_row, field)
            tx.update(case_id, **{field: new_value})
    except Exception as e:
        log_audit(
            paths.audit_log,
            "metadata",
            audit_args,
            "failure",
            case=case_id,
            details={
                "failure_kind": "exception",
                "error": traceback.format_exc(),
            },
        )
        print(
            f"internal error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 2

    # Step 6 — success audit. before_value is from the locked snapshot.
    log_audit(
        paths.audit_log,
        "metadata",
        audit_args,
        "success",
        case=case_id,
        details={
            "field": field,
            "before": before_value,
            "after": new_value,
        },
    )

    # Step 7 — render committed block to stdout.
    print(
        _render_edit_block(
            "Committed.", case_id, field, before_value, new_value
        )
    )

    # Step 8 — notes-field soft nudge.
    if field == "notes":
        print(_NOTES_NUDGE, file=sys.stderr)

    return 0


# ----- --show -----


def _show(args: Namespace, paths: NasPaths | None) -> int:
    case_id = args.ucd_fil_id
    if not _CASE_RE.match(case_id):
        print(f"error: invalid case ID format: {case_id}", file=sys.stderr)
        return 1

    if paths is None:
        paths = resolve_paths()

    manifest_by_id = {
        m.ucd_fil_id: m
        for m in CsvTable(
            paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
        ).snapshot()
    }
    manifest_row = manifest_by_id.get(case_id)
    if manifest_row is None:
        print(f"error: case not found in manifest: {case_id}", file=sys.stderr)
        return 1

    state_by_id = {
        s.ucd_fil_id: s
        for s in CsvTable(
            paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
        ).snapshot()
    }
    state_row = state_by_id.get(case_id)

    print(_render_show(manifest_row, state_row))
    return 0


def _render_show(manifest_row: CaseManifestRow, state_row: PipelineStateRow | None) -> str:
    rows: list[tuple[str, str]] = [
        ("Case:", manifest_row.ucd_fil_id),
        ("Surgeon:", manifest_row.surgeon),
        ("Case year:", manifest_row.case_year),
        ("OR room:", manifest_row.or_room),
        ("Procedure:", manifest_row.procedure_name),
        ("Approach:", manifest_row.approach),
        ("Indication:", manifest_row.indication),
        ("Notes:", manifest_row.notes if manifest_row.notes else "(empty)"),
    ]
    lines = [f"{label:<{_LABEL_WIDTH}}{value}" for label, value in rows]
    lines.append(_HRULE)
    lines.append(_stage_summary(state_row))
    return "\n".join(lines)


def _stage_summary(state_row: PipelineStateRow | None) -> str:
    if state_row is None:
        return "Pipeline stage: <not in state>"
    stage = state_row.stage
    if stage == Stage.intake:
        return f"Pipeline stage: {stage.value}"
    if stage == Stage.concatenated:
        return f"Pipeline stage: {stage.value} (concat_ts: {state_row.concat_ts})"
    if stage == Stage.deidentified:
        return f"Pipeline stage: {stage.value} (deid_ts: {state_row.deid_ts})"
    if stage == Stage.verified:
        return f"Pipeline stage: {stage.value} (verify_ts: {state_row.verify_ts})"
    if stage == Stage.failed:
        notes = state_row.verification_notes
        notes_display = notes[:_FAILED_NOTES_TRUNC] if notes else "(no notes)"
        return f"Pipeline stage: {stage.value} (notes: {notes_display})"
    return f"Pipeline stage: {stage.value}"
