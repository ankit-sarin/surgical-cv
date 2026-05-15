import json
from argparse import Namespace
from datetime import date, datetime

from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths, resolve_paths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)


_PROCEDURE_MAX = 20

_COLUMNS: list[tuple[str, int]] = [
    ("ucd_fil_id", 13),
    ("surgeon", 10),
    ("procedure", 22),
    ("segs", 5),
    ("stage", 13),
    ("concat_ts", 10),
    ("deid_ts", 10),
    ("verify_ts", 10),
]


def handle(args: Namespace, paths: NasPaths | None = None) -> int:
    if paths is None:
        paths = resolve_paths()

    manifest_table = CsvTable(
        paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow
    )
    state_table = CsvTable(
        paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow
    )

    manifest_by_id: dict[str, CaseManifestRow] = {
        m.ucd_fil_id: m for m in manifest_table.snapshot()
    }
    joined: list[tuple[PipelineStateRow, CaseManifestRow | None]] = [
        (s, manifest_by_id.get(s.ucd_fil_id)) for s in state_table.snapshot()
    ]

    if args.case is not None:
        joined = [(s, m) for (s, m) in joined if s.ucd_fil_id == args.case]
        if not joined:
            return _no_match(args)

    if args.stage is not None:
        target = Stage(args.stage)
        joined = [(s, m) for (s, m) in joined if s.stage == target]

    if args.json:
        return _render_json(joined)
    return _render_tabular(joined)


def _no_match(args: Namespace) -> int:
    if args.json:
        print(
            json.dumps(
                {"error": "case_not_found", "case": args.case}, indent=2
            )
        )
    else:
        print(f"No case found: {args.case}")
    return 0


def _fmt_ts(s: str, today: date) -> str:
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return s[:10]
    if dt.date() == today:
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%Y-%m-%d")


def _render_tabular(
    joined: list[tuple[PipelineStateRow, CaseManifestRow | None]],
) -> int:
    if not joined:
        print("(no cases match)")
        return 0

    today = date.today()
    header_line = " ".join(name.ljust(width) for name, width in _COLUMNS)
    print(header_line)
    print("-" * len(header_line))

    by_stage: dict[str, int] = {}
    for state, manifest in joined:
        surgeon = manifest.surgeon if manifest else ""
        procedure = (manifest.procedure_primary if manifest else "")[:_PROCEDURE_MAX]
        cells = [
            state.ucd_fil_id.ljust(13),
            surgeon.ljust(10),
            procedure.ljust(22),
            str(len(state.raw_segments)).rjust(5),
            state.stage.value.ljust(13),
            _fmt_ts(state.concat_ts, today).ljust(10),
            _fmt_ts(state.deid_ts, today).ljust(10),
            _fmt_ts(state.verify_ts, today).ljust(10),
        ]
        print(" ".join(cells))
        by_stage[state.stage.value] = by_stage.get(state.stage.value, 0) + 1

    parts = [f"{by_stage[s.value]} {s.value}" for s in Stage if s.value in by_stage]
    print(f"{len(joined)} cases: {', '.join(parts)}")
    return 0


def _render_json(
    joined: list[tuple[PipelineStateRow, CaseManifestRow | None]],
) -> int:
    cases = []
    by_stage: dict[str, int] = {}
    for state, manifest in joined:
        cases.append(
            {
                "ucd_fil_id": state.ucd_fil_id,
                "manifest": (
                    manifest.model_dump(mode="json") if manifest is not None else None
                ),
                "state": state.model_dump(mode="json"),
            }
        )
        by_stage[state.stage.value] = by_stage.get(state.stage.value, 0) + 1

    payload = {
        "cases": cases,
        "summary": {"total": len(cases), "by_stage": by_stage},
    }
    print(json.dumps(payload, indent=2, sort_keys=False, default=str))
    return 0
