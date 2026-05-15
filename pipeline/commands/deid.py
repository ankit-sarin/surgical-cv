import os
import sys
from argparse import Namespace
from datetime import datetime, timezone

from pipeline.audit import log_audit
from pipeline.commands._shared import format_cli_error
from pipeline.csv_io import CsvTable
from pipeline.ffmpeg import FFmpegError, ffmpeg_deid
from pipeline.paths import NasPaths, resolve_paths
from pipeline.schemas import (
    CASE_ID_RE,
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    SURGEON_RE,
    VERIFICATION_NOTES_MAX,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)


# F-016 + F-017: case-id pattern, surgeon-name pattern, and verification-notes
# truncation length all imported from pipeline.schemas (single source of truth).


def handle(args: Namespace, paths: NasPaths | None = None) -> int:
    surgeon = args.surgeon
    if not isinstance(surgeon, str) or not SURGEON_RE.match(surgeon):
        print(
            f"error: invalid surgeon name {surgeon!r}: must match {SURGEON_RE.pattern}",
            file=sys.stderr,
        )
        return 2

    if paths is None:
        paths = resolve_paths()

    manifest_table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    state_table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )

    surgeon_by_id: dict[str, str] = {
        r.ucd_fil_id: r.surgeon for r in manifest_table.snapshot()
    }

    succeeded: list[str] = []
    failed: list[str] = []

    target_case = getattr(args, "case", None)

    with state_table.transaction() as tx:
        if target_case is not None:
            if not CASE_ID_RE.match(target_case):
                print(
                    f"error: --case must match UCD-FIL-###, got {target_case!r}",
                    file=sys.stderr,
                )
                return 2

            target_row = next(
                (r for r in tx.read_all() if r.ucd_fil_id == target_case),
                None,
            )
            if target_row is None:
                print(
                    f"error: case {target_case} not found in state CSV",
                    file=sys.stderr,
                )
                return 2

            owner = surgeon_by_id.get(target_case)
            if owner is None:
                print(
                    f"error: case {target_case} has no manifest entry; "
                    "cannot verify surgeon ownership",
                    file=sys.stderr,
                )
                return 2
            if owner != surgeon:
                print(
                    f"error: case {target_case} belongs to surgeon={owner!r}, "
                    f"not {surgeon!r}",
                    file=sys.stderr,
                )
                return 2

            if target_row.stage != Stage.concatenated:
                print(
                    f"error: case {target_case} is at stage="
                    f"{target_row.stage.value!r}, expected 'concatenated'. "
                    f"Use 'status --case {target_case}' to inspect.",
                    file=sys.stderr,
                )
                return 2

            candidates = [target_row]
        else:
            candidates = [
                r
                for r in tx.read_all()
                if r.stage == Stage.concatenated
                and surgeon_by_id.get(r.ucd_fil_id) == surgeon
            ]

        if not candidates:
            print(f"No concatenated cases for surgeon={surgeon}")
            return 0

        for row in candidates:
            case_id = row.ucd_fil_id
            try:
                output_basename = _process_case(row, paths, surgeon)
            except Exception as e:
                full_error = str(e)
                error_summary = full_error[:VERIFICATION_NOTES_MAX]
                tx.update(
                    case_id,
                    stage=Stage.failed,
                    verification_notes=f"deid: {error_summary}",
                )
                log_audit(
                    paths.audit_log,
                    "deid",
                    {"surgeon": surgeon},
                    "failure",
                    case=case_id,
                    details={
                        "error": full_error,
                        "error_type": type(e).__name__,
                    },
                )
                failed.append(case_id)
                print(format_cli_error(case_id, error_summary), file=sys.stderr)
            else:
                ts = datetime.now(timezone.utc).isoformat()
                tx.update(
                    case_id,
                    stage=Stage.deidentified,
                    deid_filename=output_basename,
                    deid_ts=ts,
                )
                log_audit(
                    paths.audit_log,
                    "deid",
                    {"surgeon": surgeon},
                    "success",
                    case=case_id,
                    details={
                        "input": row.concat_filename,
                        "output": output_basename,
                    },
                )
                succeeded.append(case_id)

    total = len(succeeded) + len(failed)
    print(
        f"Processed {total} cases: {len(succeeded)} succeeded, {len(failed)} failed."
    )
    return 0 if not failed else 1


def _process_case(
    row: PipelineStateRow, paths: NasPaths, surgeon: str
) -> str:
    if not row.concat_filename:
        raise ValueError(
            "concat_filename is empty — case is at stage=concatenated but "
            "the field is unset; manual repair needed"
        )

    input_path = paths.or_raw / row.concat_filename
    if not input_path.is_file():
        raise FileNotFoundError(f"concat input not found: {input_path}")

    output_basename = f"{row.ucd_fil_id}_video.mp4"
    output_path = paths.deid_dir(surgeon) / output_basename
    partial_path = output_path.with_suffix(".partial.mp4")

    if output_path.exists():
        raise FileExistsError(
            f"deid output already exists: {output_path} — "
            "case may have been processed before"
        )
    if partial_path.exists():
        raise FileExistsError(
            f"stale partial file: {partial_path} — "
            "previous run may have crashed; inspect and remove before retry"
        )

    paths.deid_dir(surgeon).mkdir(parents=True, exist_ok=True)
    ffmpeg_deid(input_path, partial_path)

    if not partial_path.is_file() or partial_path.stat().st_size == 0:
        raise FFmpegError(
            stderr="output missing or empty after deid", exit_code=0
        )

    os.rename(partial_path, output_path)
    return output_basename
