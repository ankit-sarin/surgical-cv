"""verify subcommand — deterministic preflight + LLM diagnostician.

Per-case flow:
  1. Resolve the deid artifact path from row.deid_filename + paths.deid_dir().
  2. collect_evidence (ffprobe + exiftool + ffmpeg null-mux stderr).
  3. run_preflight: PF1 zero audio streams, PF2 no forbidden metadata fields,
     PF3 filename matches UCD-FIL-\\d{3}_video.mp4.
  4. If preflight fails: stage=failed, audit failure (no diagnostician call).
  5. If preflight passes: diagnose() against the same evidence dict, then
     transition to verified or failed based on verdict.

Exit codes:
  0 — all cases verified (or none eligible)
  1 — >=1 clean fail verdict (preflight or diagnostician fail), no infra errors
  2 — >=1 infrastructure error (malformed output after retry, or daemon
      unavailable). daemon-unavailable aborts the batch immediately.
"""

import re
import sys
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.audit import log_audit
from pipeline.commands._shared import format_cli_error
from pipeline.csv_io import CsvTable
from pipeline.diagnostician import (
    DiagnosticianInfraError,
    collect_evidence,
    diagnose,
)
from pipeline.paths import NasPaths, resolve_paths
from pipeline.schemas import (
    CASE_ID_RE,
    CASE_ID_RE_STR,
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    SURGEON_RE,
    VERIFICATION_NOTES_MAX,
    CaseManifestRow,
    DiagnosticianVerdict,
    PipelineStateRow,
    Stage,
)


# F-016 + F-017: case-id pattern, surgeon-name pattern, verification-notes
# truncation length all imported from pipeline.schemas (single source).
# _DEID_FILENAME_RE stays local — its full shape is verify-specific — but
# the case-id portion is sourced from CASE_ID_RE_STR so a future tightening
# of the case-id digit count (e.g., \d{3} → \d{4}) propagates automatically.
_CASE_ID_BODY = CASE_ID_RE_STR.removeprefix("^").removesuffix("$")
_DEID_FILENAME_RE = re.compile(rf"^{_CASE_ID_BODY}_video\.mp4$")
_ENCODER_ALLOW_RE = re.compile(r"^(Lavf|Lavc|libx264|VideoHandler|SoundHandler|GPAC).*$")

_NOTES_REASON_MAX = 160  # leaves room for "verified: " / "diagnostician: " prefix

# Forbidden top-level tag keys (case-insensitive exact match). `gps*` is handled
# as a case-insensitive prefix separately.
_FORBIDDEN_TAG_KEYS: frozenset[str] = frozenset(
    {
        "title",
        "comment",
        "artist",
        "album",
        "composer",
        "creator",
        "description",
        "genre",
        "synopsis",
        "lyrics",
        "location",
        "author",
    }
)

_ELIGIBLE_INPUT_STAGES: frozenset[Stage] = frozenset(
    {Stage.deidentified, Stage.failed}
)


@dataclass
class PreflightFailure:
    check_id: str  # "PF1" | "PF2" | "PF3"
    reason: str
    detail: dict[str, Any]


@dataclass
class PreflightResult:
    passed: bool
    failures: list[PreflightFailure]

    @property
    def first(self) -> PreflightFailure | None:
        return self.failures[0] if self.failures else None


def run_preflight(evidence: dict[str, Any], deid_basename: str) -> PreflightResult:
    """Three deterministic checks. All-or-nothing: short-circuits on first
    failure (returns a PreflightResult containing exactly one PreflightFailure
    on the failure path; empty on the pass path).
    """
    pf1 = _check_pf1_audio(evidence.get("ffprobe", {}))
    if pf1 is not None:
        return PreflightResult(passed=False, failures=[pf1])

    pf2 = _check_pf2_metadata(
        evidence.get("ffprobe", {}), evidence.get("exiftool", {})
    )
    if pf2 is not None:
        return PreflightResult(passed=False, failures=[pf2])

    pf3 = _check_pf3_filename(deid_basename)
    if pf3 is not None:
        return PreflightResult(passed=False, failures=[pf3])

    return PreflightResult(passed=True, failures=[])


def _check_pf1_audio(ffprobe_json: dict[str, Any]) -> PreflightFailure | None:
    streams = ffprobe_json.get("streams", []) or []
    audio_count = sum(1 for s in streams if s.get("codec_type") == "audio")
    if audio_count > 0:
        return PreflightFailure(
            check_id="PF1",
            reason=f"audio streams present (count={audio_count})",
            detail={"audio_stream_count": audio_count},
        )
    return None


def _check_pf2_metadata(
    ffprobe_json: dict[str, Any], exiftool_json: dict[str, Any]
) -> PreflightFailure | None:
    sources: list[tuple[str, dict[str, Any]]] = []
    format_tags = ffprobe_json.get("format", {}).get("tags") or {}
    sources.append(("format.tags", format_tags))
    for i, stream in enumerate(ffprobe_json.get("streams", []) or []):
        stream_tags = stream.get("tags") or {}
        sources.append((f"streams[{i}].tags", stream_tags))
    sources.append(("exiftool", exiftool_json or {}))

    for source_name, tags in sources:
        for key, value in tags.items():
            key_lc = key.lower()
            if key_lc in _FORBIDDEN_TAG_KEYS:
                return PreflightFailure(
                    check_id="PF2",
                    reason=f"forbidden tag '{key}' present in {source_name}",
                    detail={
                        "source": source_name,
                        "field": key,
                        "value": str(value)[:120],
                    },
                )
            if key_lc.startswith("gps"):
                return PreflightFailure(
                    check_id="PF2",
                    reason=f"GPS-shaped tag '{key}' present in {source_name}",
                    detail={
                        "source": source_name,
                        "field": key,
                        "value": str(value)[:120],
                    },
                )
            if key_lc in ("encoder", "handler_name"):
                str_value = str(value).strip()
                if str_value and not _ENCODER_ALLOW_RE.match(str_value):
                    return PreflightFailure(
                        check_id="PF2",
                        reason=(
                            f"disallowed {key}={str_value!r} in {source_name}"
                        ),
                        detail={
                            "source": source_name,
                            "field": key,
                            "value": str_value[:120],
                        },
                    )

    return None


def _check_pf3_filename(deid_basename: str) -> PreflightFailure | None:
    if not _DEID_FILENAME_RE.match(deid_basename):
        return PreflightFailure(
            check_id="PF3",
            reason=f"filename {deid_basename!r} does not match UCD-FIL-###_video.mp4",
            detail={"filename": deid_basename},
        )
    return None


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

    verified: list[str] = []
    failed: list[str] = []
    infra_errors: list[str] = []

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

            if target_row.stage not in _ELIGIBLE_INPUT_STAGES:
                print(
                    f"error: case {target_case} is at stage="
                    f"{target_row.stage.value!r}, expected one of "
                    f"{sorted(s.value for s in _ELIGIBLE_INPUT_STAGES)!r}. "
                    f"Use 'status --case {target_case}' to inspect.",
                    file=sys.stderr,
                )
                return 2

            candidates = [target_row]
        else:
            candidates = [
                r
                for r in tx.read_all()
                if r.stage in _ELIGIBLE_INPUT_STAGES
                and surgeon_by_id.get(r.ucd_fil_id) == surgeon
            ]

        if not candidates:
            print(f"No eligible cases for surgeon={surgeon}")
            return 0

        daemon_aborted = False

        for row in candidates:
            case_id = row.ucd_fil_id
            try:
                outcome_kind, payload = _process_case(row, paths, surgeon)
            except DiagnosticianInfraError as e:
                if e.reason == "ollama_unavailable":
                    log_audit(
                        paths.audit_log,
                        "verify",
                        {"surgeon": surgeon},
                        "failure",
                        case=case_id,
                        details={
                            "failure_kind": "infra",
                            "infra_reason": "ollama_unavailable",
                            "error": e.error,
                        },
                    )
                    infra_errors.append(case_id)
                    print(
                        f"  {case_id}: ABORTED — Ollama daemon unavailable: {e.error}",
                        file=sys.stderr,
                    )
                    daemon_aborted = True
                    break
                # malformed_output: state unchanged, verify_ts NOT written
                log_audit(
                    paths.audit_log,
                    "verify",
                    {"surgeon": surgeon},
                    "failure",
                    case=case_id,
                    details={
                        "failure_kind": "infra",
                        "infra_reason": e.reason,
                        "raw_outputs": e.raw_outputs,
                    },
                )
                infra_errors.append(case_id)
                print(
                    f"  {case_id}: INFRA ERROR — {e.reason} (state unchanged)",
                    file=sys.stderr,
                )
                continue
            except Exception as e:
                full_error = str(e)
                error_summary = full_error[:VERIFICATION_NOTES_MAX]
                tx.update(
                    case_id,
                    stage=Stage.failed,
                    verification_notes=f"verify: {error_summary}",
                )
                log_audit(
                    paths.audit_log,
                    "verify",
                    {"surgeon": surgeon},
                    "failure",
                    case=case_id,
                    details={
                        "failure_kind": "exception",
                        "error": full_error,
                        "error_type": type(e).__name__,
                    },
                )
                failed.append(case_id)
                print(format_cli_error(case_id, error_summary), file=sys.stderr)
                continue

            ts = datetime.now(timezone.utc).isoformat()
            if outcome_kind == "verified":
                verdict: DiagnosticianVerdict = payload
                notes = f"verified: {verdict.reason[:_NOTES_REASON_MAX]}"
                tx.update(
                    case_id,
                    stage=Stage.verified,
                    verify_ts=ts,
                    verification_notes=notes[:VERIFICATION_NOTES_MAX],
                )
                log_audit(
                    paths.audit_log,
                    "verify",
                    {"surgeon": surgeon},
                    "success",
                    case=case_id,
                    details={
                        "preflight_passed": True,
                        "verdict": verdict.model_dump(mode="json"),
                    },
                )
                verified.append(case_id)
                print(f"  {case_id}: VERIFIED — {verdict.reason}")
            elif outcome_kind == "preflight_failed":
                pf: PreflightFailure = payload
                notes = f"preflight {pf.check_id}: {pf.reason[:_NOTES_REASON_MAX]}"
                tx.update(
                    case_id,
                    stage=Stage.failed,
                    verify_ts=ts,
                    verification_notes=notes[:VERIFICATION_NOTES_MAX],
                )
                log_audit(
                    paths.audit_log,
                    "verify",
                    {"surgeon": surgeon},
                    "failure",
                    case=case_id,
                    details={
                        "failure_kind": "preflight",
                        "preflight_passed": False,
                        "check_id": pf.check_id,
                        "reason": pf.reason,
                        "offending": pf.detail,
                    },
                )
                failed.append(case_id)
                print(
                    f"  {case_id}: FAILED preflight {pf.check_id} — {pf.reason}",
                    file=sys.stderr,
                )
            elif outcome_kind == "diagnostician_failed":
                verdict = payload
                notes = (
                    f"diagnostician: {verdict.reason[:_NOTES_REASON_MAX + 20]}"
                )
                tx.update(
                    case_id,
                    stage=Stage.failed,
                    verify_ts=ts,
                    verification_notes=notes[:VERIFICATION_NOTES_MAX],
                )
                log_audit(
                    paths.audit_log,
                    "verify",
                    {"surgeon": surgeon},
                    "failure",
                    case=case_id,
                    details={
                        "failure_kind": "diagnostician",
                        "preflight_passed": True,
                        "verdict": verdict.model_dump(mode="json"),
                    },
                )
                failed.append(case_id)
                print(
                    f"  {case_id}: FAILED diagnostician — {verdict.reason}",
                    file=sys.stderr,
                )

    total = len(verified) + len(failed) + len(infra_errors)
    print(
        f"Processed {total} cases: {len(verified)} verified, "
        f"{len(failed)} failed, {len(infra_errors)} infra errors."
    )
    if infra_errors:
        return 2
    if failed:
        return 1
    return 0


def _process_case(
    row: PipelineStateRow, paths: NasPaths, surgeon: str
) -> tuple[str, Any]:
    """Returns one of:
        ("verified", DiagnosticianVerdict)
        ("preflight_failed", PreflightFailure)
        ("diagnostician_failed", DiagnosticianVerdict)
    Raises DiagnosticianInfraError on Ollama infra failure (caller decides
    whether to abort batch). Raises FileNotFoundError or ValueError on
    deid-artifact problems.
    """
    if not row.deid_filename:
        raise ValueError(
            "deid_filename is empty — case is at stage=deidentified/failed "
            "but the field is unset; manual repair needed"
        )

    deid_path = paths.deid_dir(surgeon) / row.deid_filename
    if not deid_path.is_file():
        raise FileNotFoundError(f"deid artifact not found: {deid_path}")

    evidence = collect_evidence(deid_path)

    preflight = run_preflight(evidence, deid_path.name)
    if not preflight.passed:
        return ("preflight_failed", preflight.first)

    verdict = diagnose(evidence)
    if verdict.verdict == "pass":
        return ("verified", verdict)
    return ("diagnostician_failed", verdict)
