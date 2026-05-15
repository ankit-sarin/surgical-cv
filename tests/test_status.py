import json
import os
from argparse import Namespace
from pathlib import Path

from pipeline.commands import status as status_mod
from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)


def _make_paths(tmp_path: Path) -> NasPaths:
    root = tmp_path / "nas"
    or_raw = root / "or-raw"
    or_raw.mkdir(parents=True)
    return NasPaths(
        root=root,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )


def _manifest(ucd_fil_id="UCD-FIL-001", surgeon="sarin", procedure_primary="Sigmoidectomy", **kw):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        surgeon=surgeon,
        case_year="2026",
        or_room="OR4",
        procedure_primary=procedure_primary,
        approach="Robotic",
        indication="Diverticulitis",
        notes="",
    )
    base.update(kw)
    return CaseManifestRow(**base)


def _state(ucd_fil_id="UCD-FIL-001", stage=Stage.intake, **kw):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        raw_segments=["a.mp4"],
        concat_filename="",
        deid_filename="",
        stage=stage,
        concat_ts="",
        deid_ts="",
        verify_ts="",
        verification_notes="",
    )
    base.update(kw)
    return PipelineStateRow(**base)


def _seed_manifest(paths: NasPaths, *rows):
    t = CsvTable(paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow)
    with t.transaction() as tx:
        for r in rows:
            tx.append(r)


def _seed_state(paths: NasPaths, *rows):
    t = CsvTable(paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow)
    with t.transaction() as tx:
        for r in rows:
            tx.append(r)


def _args(case=None, stage=None, json_mode=False):
    return Namespace(case=case, stage=stage, json=json_mode)


def test_empty_everything_tabular(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    rc = status_mod.handle(_args(), paths=paths)
    assert rc == 0
    assert capsys.readouterr().out.strip() == "(no cases match)"


def test_empty_everything_json(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    rc = status_mod.handle(_args(json_mode=True), paths=paths)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"cases": [], "summary": {"total": 0, "by_stage": {}}}


def test_one_case_intake_tabular(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest())
    _seed_state(paths, _state())
    rc = status_mod.handle(_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("ucd_fil_id")
    assert "UCD-FIL-001" in lines[2]
    assert "sarin" in lines[2]
    assert "Sigmoidectomy" in lines[2]
    assert "intake" in lines[2]
    assert lines[-1] == "1 cases: 1 intake"


def test_four_cases_mixed_stages_tabular(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(
        paths,
        _manifest("UCD-FIL-001"),
        _manifest("UCD-FIL-002"),
        _manifest("UCD-FIL-003"),
        _manifest("UCD-FIL-004"),
    )
    _seed_state(
        paths,
        _state("UCD-FIL-001", stage=Stage.intake),
        _state("UCD-FIL-002", stage=Stage.concatenated, concat_ts="2026-05-12T10:00:00"),
        _state("UCD-FIL-003", stage=Stage.deidentified, concat_ts="2025-12-15T10:00:00"),
        _state("UCD-FIL-004", stage=Stage.failed),
    )
    rc = status_mod.handle(_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    data_lines = [ln for ln in lines if ln.startswith("UCD-FIL-")]
    assert len(data_lines) == 4
    footer = lines[-1]
    assert footer.startswith("4 cases:")
    assert "1 intake" in footer
    assert "1 concatenated" in footer
    assert "1 deidentified" in footer
    assert "1 failed" in footer
    intake_idx = next(i for i, s in enumerate(["intake", "concatenated", "deidentified", "verified", "failed"]) if s == "intake")
    concat_idx = next(i for i, s in enumerate(["intake", "concatenated", "deidentified", "verified", "failed"]) if s == "concatenated")
    assert footer.index("intake") < footer.index("concatenated") if intake_idx < concat_idx else True


def test_filter_by_case_matches(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"), _manifest("UCD-FIL-002"))
    _seed_state(paths, _state("UCD-FIL-001"), _state("UCD-FIL-002"))
    rc = status_mod.handle(_args(case="UCD-FIL-001"), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "UCD-FIL-001" in out
    assert "UCD-FIL-002" not in out
    assert out.splitlines()[-1] == "1 cases: 1 intake"


def test_filter_by_case_no_match_tabular(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"))
    _seed_state(paths, _state("UCD-FIL-001"))
    rc = status_mod.handle(_args(case="UCD-FIL-999"), paths=paths)
    assert rc == 0
    assert capsys.readouterr().out.strip() == "No case found: UCD-FIL-999"


def test_filter_by_stage(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"), _manifest("UCD-FIL-002"), _manifest("UCD-FIL-003"))
    _seed_state(
        paths,
        _state("UCD-FIL-001", stage=Stage.intake),
        _state("UCD-FIL-002", stage=Stage.concatenated),
        _state("UCD-FIL-003", stage=Stage.concatenated),
    )
    rc = status_mod.handle(_args(stage="concatenated"), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "UCD-FIL-001" not in out
    assert "UCD-FIL-002" in out
    assert "UCD-FIL-003" in out
    assert out.splitlines()[-1] == "2 cases: 2 concatenated"


def test_filter_case_and_stage_and_applied(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"))
    _seed_state(paths, _state("UCD-FIL-001", stage=Stage.concatenated))
    rc = status_mod.handle(_args(case="UCD-FIL-001", stage="intake"), paths=paths)
    assert rc == 0
    assert capsys.readouterr().out.strip() == "(no cases match)"


def test_state_row_with_no_manifest_entry(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_state(paths, _state("UCD-FIL-001"))
    rc = status_mod.handle(_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "UCD-FIL-001" in out

    rc = status_mod.handle(_args(json_mode=True), paths=paths)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["cases"]) == 1
    assert payload["cases"][0]["manifest"] is None
    assert payload["cases"][0]["state"]["ucd_fil_id"] == "UCD-FIL-001"


def test_json_valid_with_real_data(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"))
    _seed_state(paths, _state("UCD-FIL-001", stage=Stage.concatenated, concat_ts="2026-05-12T10:00:00"))
    rc = status_mod.handle(_args(json_mode=True), paths=paths)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload["cases"], list)
    assert len(payload["cases"]) == 1
    c = payload["cases"][0]
    assert c["ucd_fil_id"] == "UCD-FIL-001"
    assert c["manifest"]["surgeon"] == "sarin"
    assert c["state"]["stage"] == "concatenated"
    assert c["state"]["raw_segments"] == ["a.mp4"]
    assert payload["summary"] == {"total": 1, "by_stage": {"concatenated": 1}}


def test_json_no_match_shape(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    rc = status_mod.handle(_args(case="UCD-FIL-999", json_mode=True), paths=paths)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "case_not_found", "case": "UCD-FIL-999"}


def test_no_audit_log_written(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"))
    _seed_state(paths, _state("UCD-FIL-001"))
    assert not paths.audit_log.exists()
    status_mod.handle(_args(), paths=paths)
    status_mod.handle(_args(json_mode=True), paths=paths)
    status_mod.handle(_args(case="UCD-FIL-001"), paths=paths)
    status_mod.handle(_args(case="UCD-FIL-999"), paths=paths)
    status_mod.handle(_args(stage="concatenated"), paths=paths)
    assert not paths.audit_log.exists()


def test_status_creates_no_new_files(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest("UCD-FIL-001"))
    _seed_state(paths, _state("UCD-FIL-001"))
    before = sorted(os.listdir(paths.or_raw))
    for args in [
        _args(),
        _args(json_mode=True),
        _args(case="UCD-FIL-001"),
        _args(case="UCD-FIL-999"),
        _args(stage="concatenated"),
        _args(stage="intake", json_mode=True),
    ]:
        status_mod.handle(args, paths=paths)
    after = sorted(os.listdir(paths.or_raw))
    assert before == after


def test_long_procedure_name_truncated(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    long = "x" * 50
    _seed_manifest(paths, _manifest("UCD-FIL-001", procedure_primary=long))
    _seed_state(paths, _state("UCD-FIL-001"))
    rc = status_mod.handle(_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert ("x" * 20) in out
    assert ("x" * 21) not in out
