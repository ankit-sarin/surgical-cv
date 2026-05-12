import os
import re
import sys
from argparse import Namespace
from datetime import datetime, timezone

from pipeline.audit import log_audit
from pipeline.csv_io import CsvTable
from pipeline.ffmpeg import (
    FFmpegError,
    check_uniformity,
    ffmpeg_concat,
    parse_bdv_timestamp,
)
from pipeline.paths import NasPaths, resolve_paths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)


_SURGEON_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_VERIFICATION_NOTES_MAX = 200


def handle(args: Namespace, paths: NasPaths | None = None) -> int:
    surgeon = args.surgeon
    if not isinstance(surgeon, str) or not _SURGEON_RE.match(surgeon):
        print(
            f"error: invalid surgeon name {surgeon!r}: must match {_SURGEON_RE.pattern}",
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

    with state_table.transaction() as tx:
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
                error_summary = full_error[:_VERIFICATION_NOTES_MAX]
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
                print(
                    f"  {case_id}: FAILED — {error_summary}",
                    file=sys.stderr,
                )
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
