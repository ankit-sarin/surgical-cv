"""metadata subcommand — read, dry-run preview, and commit edits to
case_manifest.csv.

Three dispatch paths from run(args):
  --show (or no flags)        read-only render of all 10 manifest fields
                              plus a one-line pipeline-stage summary.
                              Silent on the audit log. Notes are rendered
                              as ``<redacted, length=N>`` (F-004) — the
                              actual notes content never appears on stdout.
  --edit FIELD VALUE          dry-run: validates and previews "DRY RUN — ..."
                              with Before/After. Silent on the audit log.
  --edit FIELD VALUE --confirm commit path: opens CsvTable.transaction(),
                              captures `before` from the locked snapshot
                              (NOT the pre-transaction snapshot), writes
                              one cell, and logs exactly one audit entry
                              with failure_kind ∈ {format, not_found,
                              validation, infra, exception} on failure or
                              outcome="success" on commit.

F-003: ``--edit notes`` (with or without ``--confirm``) is refused at
dispatch time. The CLI exists for operator-side schema corrections;
free-text notes belong in the surgeon UI when it's built. The block is
policy, not validation — non-zero exit, message on stderr, no audit
entry, no manifest read.

The five failure_kind values are metadata-specific and live alongside
verify's existing discriminators in the audit log; audit.py's schema is
unchanged.
"""

import json
import re
import sys
import traceback
from argparse import Namespace

from pipeline.audit import log_audit
from pipeline.csv_io import CsvTable, RowNotFoundError
from pipeline.paths import NasPaths, resolve_paths
from pipeline.phi_redact import redact_field
from pipeline.picklists import PicklistError, load_picklist_values
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)


# F-003: notes is operator-blocked at the CLI. The metadata CLI exists for
# operator-side schema corrections (procedure type, OR room, case_year, etc.).
# Free-text notes editing belongs in the surgeon UI when it's built; until
# then the door stays closed so the audit-log redaction question never arises.
_NOTES_BLOCKED_FIELD = "notes"
_NOTES_BLOCKED_MSG = (
    "notes is not editable via CLI; use the surgeon interface."
)


_CASE_RE = re.compile(r"^UCD-FIL-\d{3}$")
# Mirrors CaseManifestRow.case_year Field(pattern=r"^\d{4}$") in pipeline/schemas.py.
_YEAR_RE = re.compile(r"^\d{4}$")

_LABEL_WIDTH = 16
_HRULE = "─" * 14
_FAILED_NOTES_TRUNC = 80

# Field name → picklist vocab name. case_year handles its own validation
# branch (regex + vocab) but its vocab still comes from the same loader via
# _PICKLIST_SPECIALTIES below, so it's NOT listed here.
# conversion_target shares the approach vocabulary (same picklist, different
# column on the manifest); procedure_additional reuses the procedure vocab
# in a per-element check rather than a single-string lookup.
_PICKLIST_FIELDS: dict[str, str] = {
    "procedure_primary": "procedure",
    "approach": "approach",
    "conversion_target": "approach",
    "indication": "indication",
}

# Picklist vocab name → specialty for the seed-file lookup. All four routes
# go through pipeline.picklists.load_picklist_values. specialty=None means
# the universal `<field>.json` seed file. Hardcoded per field for now; when a
# second specialty lands, refactor to a per-user lookup at validation time.
_PICKLIST_SPECIALTIES: dict[str, str | None] = {
    "procedure": "colorectal",
    "approach": None,
    "indication": "colorectal",
    "case_year": None,
}

# Plural display labels for the user-facing validation error message. Keeps
# the error English-natural now that all vocab names are singular.
_PICKLIST_LABELS: dict[str, str] = {
    "procedure": "procedures",
    "approach": "approaches",
    "indication": "indications",
    "case_year": "case_years",
}

class _InfraError(Exception):
    """Vocab-load failure or other infrastructure-level problem. Exit 2."""


def _load_vocab(name: str) -> list[str]:
    if name not in _PICKLIST_SPECIALTIES:
        raise _InfraError(f"unknown picklist field: {name}")
    specialty = _PICKLIST_SPECIALTIES[name]
    try:
        return load_picklist_values(name, specialty=specialty)
    except PicklistError as e:
        raise _InfraError(str(e)) from e


def run(args: Namespace, paths: NasPaths | None = None) -> int:
    """Dispatch metadata subcommand by argparse flags.

    Routes --edit + --confirm to the commit path, --edit alone to the
    dry-run preview, and the no-flag / --show form to the read-only
    render. See module docstring for the audit-log policy on each path.

    F-003 short-circuit: --edit notes (with or without --confirm) is
    refused at dispatch time before any manifest read, vocab load, or
    audit write. The block is policy, not a validation error — no
    audit entry, just a policy message on stderr and a non-zero exit.
    """
    if args.edit is not None:
        field, _value = args.edit
        if field == _NOTES_BLOCKED_FIELD:
            print(f"error: {_NOTES_BLOCKED_MSG}", file=sys.stderr)
            return 1
        if args.confirm:
            return _commit(args, paths)
        return _dry_run(args, paths)
    return _show(args, paths)


# ----- shared helpers -----


def _audit_args(args: Namespace) -> dict:
    field, value = args.edit
    return {"edit_field": field, "edit_value": value, "confirm": True}


def _validate_field(
    field: str,
    value: str,
    current_row: CaseManifestRow | None = None,
) -> tuple[str | None, object]:
    """Return ``(error_or_none, coerced_value)``. The coerced value is what
    callers must pass to ``tx.update`` — for most fields it's the raw string,
    but ``procedure_additional`` coerces to ``list[str]``.

    ``current_row`` carries the pre-edit row for cross-field rules
    (``conversion_target`` must not equal the existing ``approach``).
    May raise _InfraError on vocab-load problems (caller maps that to exit 2).
    """
    if field == "case_year":
        if not _YEAR_RE.match(value):
            return f"expected 4-digit year, got {value!r}", value
        vocab = _load_vocab("case_year")
        if value not in vocab:
            # Order-agnostic range display: vocab may be sorted DESC for UX.
            return (
                f"year {value!r} not in case_years allowlist "
                f"({len(vocab)} allowed: {min(vocab)}-{max(vocab)})",
                value,
            )
        return None, value
    if field == "or_room":
        if value.strip() == "":
            return "or_room must be non-empty", value
        return None, value
    if field == "notes":
        return None, value
    if field == "conversion_target":
        # Empty = clear the conversion. Skip vocab + cross-field checks.
        if value == "":
            return None, value
        vocab = _load_vocab("approach")
        if value not in vocab:
            label = _PICKLIST_LABELS["approach"]
            return (
                f"{value!r} not in {label} vocabulary "
                f"({len(vocab)} allowed values)",
                value,
            )
        if current_row is not None and value == current_row.approach:
            return (
                f"conversion_target {value!r} equals the case's "
                f"primary approach; pick a different target or clear "
                f"conversion_target",
                value,
            )
        return None, value
    if field == "procedure_additional":
        return _validate_additionals(value, current_row)
    if field in _PICKLIST_FIELDS:
        vocab_name = _PICKLIST_FIELDS[field]
        vocab = _load_vocab(vocab_name)
        if value not in vocab:
            label = _PICKLIST_LABELS[vocab_name]
            return (
                f"{value!r} not in {label} vocabulary "
                f"({len(vocab)} allowed values)",
                value,
            )
        return None, value
    return f"unknown field {field!r}", value


def _validate_additionals(
    value: str, current_row: CaseManifestRow | None
) -> tuple[str | None, object]:
    """Parse + validate the procedure_additional JSON-array string.

    Rules:
      - "" coerces to [] (a case with no additionals).
      - Otherwise: must be a JSON array of strings (each non-empty).
      - Each element must be in the procedure picklist.
      - No element may duplicate procedure_primary on the row.
      - No internal duplicates.
    """
    if value == "":
        return None, []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        return f"procedure_additional is not valid JSON: {e.msg}", value
    if not isinstance(parsed, list):
        return (
            f"procedure_additional must be a JSON array, "
            f"got {type(parsed).__name__}",
            value,
        )
    for item in parsed:
        if not isinstance(item, str) or not item:
            return (
                "procedure_additional elements must be non-empty strings",
                value,
            )
    vocab = _load_vocab("procedure")
    label = _PICKLIST_LABELS["procedure"]
    for item in parsed:
        if item not in vocab:
            return (
                f"{item!r} not in {label} vocabulary "
                f"({len(vocab)} allowed values)",
                value,
            )
    if current_row is not None and current_row.procedure_primary in parsed:
        return (
            f"procedure_additional contains the primary procedure "
            f"{current_row.procedure_primary!r}; each procedure may "
            f"appear only once per case",
            value,
        )
    seen: set[str] = set()
    for item in parsed:
        if item in seen:
            return (
                f"procedure_additional contains duplicate value {item!r}",
                value,
            )
        seen.add(item)
    return None, parsed


def _format_field_value(value: object) -> str:
    """Stringify a manifest field for display in dry-run / commit / show
    blocks. Lists (procedure_additional) round-trip via JSON; "" → "(empty)";
    empty lists render as "(empty)"."""
    if isinstance(value, list):
        return json.dumps(value) if value else "(empty)"
    if value == "":
        return "(empty)"
    return str(value)


def _render_edit_block(
    header: str,
    case_id: str,
    field: str,
    before: object,
    after: object,
) -> str:
    return "\n".join(
        [
            header,
            f"{'Case:':<{_LABEL_WIDTH}}{case_id}",
            f"{'Field:':<{_LABEL_WIDTH}}{field}",
            f"{'Before:':<{_LABEL_WIDTH}}{_format_field_value(before)}",
            f"{'After:':<{_LABEL_WIDTH}}{_format_field_value(after)}",
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
        reason, coerced = _validate_field(field, value, current_row=manifest_row)
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
            coerced,
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

    # Steps 3 + 4 — vocab load and value validation. The pre-snapshot row
    # is what feeds cross-field rules (e.g., conversion_target vs approach);
    # any concurrent mutation between here and the locked snapshot in step 5
    # is a race the single-user metadata CLI accepts.
    pre_row = manifest_by_id[case_id]
    try:
        reason, coerced = _validate_field(field, new_value, current_row=pre_row)
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
    before_value: object | None = None
    try:
        with manifest_table.transaction() as tx:
            locked_row = tx.find(case_id)
            if locked_row is None:
                # Race: case vanished between pre-snapshot and lock acquire.
                # Treated as exception so the transaction does not commit.
                raise RowNotFoundError(case_id)
            before_value = getattr(locked_row, field)
            tx.update(case_id, **{field: coerced})
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
            "after": coerced,
        },
    )

    # Step 7 — render committed block to stdout.
    print(
        _render_edit_block(
            "Committed.", case_id, field, before_value, coerced
        )
    )

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
    additional_disp = (
        ", ".join(manifest_row.procedure_additional)
        if manifest_row.procedure_additional
        else "(none)"
    )
    conv_disp = manifest_row.conversion_target or "(none)"
    rows: list[tuple[str, str]] = [
        ("Case:", manifest_row.ucd_fil_id),
        ("Surgeon:", manifest_row.surgeon),
        ("Case year:", manifest_row.case_year),
        ("OR room:", manifest_row.or_room),
        ("Procedure:", manifest_row.procedure_primary),
        ("Additional:", additional_disp),
        ("Approach:", manifest_row.approach),
        ("Conversion:", conv_disp),
        ("Indication:", manifest_row.indication),
        # F-004: notes never render in cleartext on stdout — journalctl /
        # operator terminal capture is the leak surface. Operators who need
        # the actual text read case_manifest.csv directly (file access,
        # not terminal output, doesn't hit journalctl).
        ("Notes:", redact_field("notes", manifest_row.notes)),
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
