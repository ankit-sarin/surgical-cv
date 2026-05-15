import os
import sys
from argparse import Namespace
from datetime import datetime, timezone

from pipeline.audit import log_audit
from pipeline.commands._shared import format_cli_error
from pipeline.csv_io import CsvTable
from pipeline.ffmpeg import (
    FFmpegError,
    check_uniformity,
    ffmpeg_concat,
    parse_bdv_timestamp,
)
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


# F-017: surgeon-name pattern + verification-notes truncation imported from
# pipeline.schemas (single source of truth across concat / deid / verify).


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
            # F-022: per-case concat. Mirrors the existing --case shape on
            # deid / verify so the worker can dispatch markers individually
            # instead of batch-by-surgeon. A failure on one case no longer
            # rolls back the transaction for siblings in the same iteration.
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

            if target_row.stage != Stage.intake:
                print(
                    f"error: case {target_case} is at stage="
                    f"{target_row.stage.value!r}, expected 'intake'. "
                    f"Use 'status --case {target_case}' to inspect.",
                    file=sys.stderr,
                )
                return 2

            candidates = [target_row]
        else:
            candidates = [
                r
                for r in tx.read_all()
                if r.stage == Stage.intake
                and surgeon_by_id.get(r.ucd_fil_id) == surgeon
            ]
        if not candidates:
            print(f"No intake cases for surgeon={surgeon}")
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
                    verification_notes=f"concat: {error_summary}",
                )
                log_audit(
                    paths.audit_log,
                    "concat",
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
                    stage=Stage.concatenated,
                    concat_filename=output_basename,
                    concat_ts=ts,
                )
                log_audit(
                    paths.audit_log,
                    "concat",
                    {"surgeon": surgeon},
                    "success",
                    case=case_id,
                    details={
                        "segments": len(row.raw_segments),
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
    raw_dir = paths.raw_dir(surgeon)
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"raw directory not found: {raw_dir}")

    segments = [raw_dir / seg for seg in row.raw_segments]
    for seg in segments:
        if not seg.is_file():
            raise FileNotFoundError(f"segment not found: {seg}")

    check_uniformity(segments)

    first_ts = parse_bdv_timestamp(segments[0].name)
    output_basename = f"{surgeon}_{first_ts}.mp4"
    output_path = paths.or_raw / output_basename
    partial_path = output_path.with_suffix(".partial.mp4")

    if output_path.exists():
        raise FileExistsError(
            f"concat output already exists: {output_path} — "
            "case may have been processed before"
        )
    if partial_path.exists():
        raise FileExistsError(
            f"stale partial file: {partial_path} — "
            "previous run may have crashed; inspect and remove before retry"
        )

    paths.or_raw.mkdir(parents=True, exist_ok=True)
    ffmpeg_concat(segments, partial_path)

    if not partial_path.is_file() or partial_path.stat().st_size == 0:
        raise FFmpegError(
            stderr="output missing or empty after concat", exit_code=0
        )

    os.rename(partial_path, output_path)

    for seg in segments:
        copied = seg.with_name(seg.stem + "-copied" + seg.suffix)
        try:
            os.rename(seg, copied)
        except OSError as e:
            print(
                f"WARNING: failed to rename segment {seg} → {copied}: {e}",
                file=sys.stderr,
            )

    return output_basename
