import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pipeline.commands import deid as deid_mod
from pipeline.csv_io import CsvTable
from pipeline.ffmpeg import FFmpegError
from pipeline.paths import NasPaths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


def _manifest_row(ucd_fil_id="UCD-FIL-001", surgeon="sarin", **kw):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        surgeon=surgeon,
        case_year="2026",
        or_room="OR4",
        procedure_name="Sigmoidectomy",
        approach="Robotic",
        indication="Diverticulitis",
        notes="",
    )
    base.update(kw)
    return CaseManifestRow(**base)


def _state_row(
    ucd_fil_id="UCD-FIL-001",
    stage=Stage.concatenated,
    concat_filename="sarin_20260101-080000.mp4",
    **kw,
):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        raw_segments=["capt0_20260101-080000.mp4"],
        concat_filename=concat_filename,
        deid_filename="",
        stage=stage,
        concat_ts="2026-05-12T10:00:00+00:00",
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


def _state_rows(paths: NasPaths) -> dict[str, PipelineStateRow]:
    t = CsvTable(paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow)
    return {r.ucd_fil_id: r for r in t.snapshot()}


def _make_concat_file(paths: NasPaths, name: str) -> Path:
    p = paths.or_raw / name
    p.write_bytes(b"fake concat output")
    return p


def _good_deid(_input, output):
    Path(output).write_bytes(b"fake deid output")


def _empty_deid(_input, output):
    Path(output).write_bytes(b"")


def _patch(monkeypatch, *, deid=_good_deid):
    monkeypatch.setattr(deid_mod, "ffmpeg_deid", deid)


def _audit_entries(paths: NasPaths) -> list[dict]:
    if not paths.audit_log.exists():
        return []
    return [json.loads(line) for line in paths.audit_log.read_text().splitlines()]


def test_no_concatenated_cases_for_surgeon(tmp_path, capsys, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    assert "No concatenated cases for surgeon=sarin" in capsys.readouterr().out
    assert not paths.audit_log.exists()


def test_single_happy_path(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.deidentified
    assert row.deid_filename == "UCD-FIL-001_video.mp4"
    assert row.deid_ts != ""

    output = paths.deid_dir("sarin") / "UCD-FIL-001_video.mp4"
    assert output.exists()
    assert output.read_bytes() == b"fake deid output"

    entries = _audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["command"] == "deid"
    assert e["outcome"] == "success"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["input"] == "sarin_20260101-080000.mp4"
    assert e["details"]["output"] == "UCD-FIL-001_video.mp4"


def test_two_cases_both_succeed(tmp_path, monkeypatch, capsys):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _make_concat_file(paths, "sarin_20260102-080000.mp4")
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001"),
        _manifest_row("UCD-FIL-002"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", concat_filename="sarin_20260101-080000.mp4"),
        _state_row("UCD-FIL-002", concat_filename="sarin_20260102-080000.mp4"),
    )
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.deidentified
    assert rows["UCD-FIL-002"].stage == Stage.deidentified
    out = capsys.readouterr().out
    assert "Processed 2 cases: 2 succeeded, 0 failed." in out
    entries = _audit_entries(paths)
    assert len([e for e in entries if e["outcome"] == "success"]) == 2


def test_success_plus_ffmpeg_failure_mix(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _make_concat_file(paths, "sarin_20260102-080000.mp4")
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001"),
        _manifest_row("UCD-FIL-002"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", concat_filename="sarin_20260101-080000.mp4"),
        _state_row("UCD-FIL-002", concat_filename="sarin_20260102-080000.mp4"),
    )

    counter = {"n": 0}

    def fake_deid(input_path, output):
        counter["n"] += 1
        if counter["n"] == 2:
            raise FFmpegError(stderr="encode failure", exit_code=1)
        Path(output).write_bytes(b"fake deid output")

    _patch(monkeypatch, deid=fake_deid)
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.deidentified
    assert rows["UCD-FIL-002"].stage == Stage.failed
    assert rows["UCD-FIL-002"].verification_notes.startswith("deid:")
    entries = {e["case"]: e for e in _audit_entries(paths)}
    assert entries["UCD-FIL-001"]["outcome"] == "success"
    assert entries["UCD-FIL-002"]["outcome"] == "failure"
    assert entries["UCD-FIL-002"]["details"]["error_type"] == "FFmpegError"


def test_missing_input_file(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001", concat_filename="nope.mp4"))
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "concat input not found" in row.verification_notes
    entries = _audit_entries(paths)
    assert entries[0]["details"]["error_type"] == "FileNotFoundError"


def test_empty_concat_filename(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001", concat_filename=""))
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "concat_filename is empty" in row.verification_notes
    entries = _audit_entries(paths)
    assert entries[0]["details"]["error_type"] == "ValueError"


def test_output_collision(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    paths.deid_dir("sarin").mkdir(parents=True)
    (paths.deid_dir("sarin") / "UCD-FIL-001_video.mp4").write_bytes(b"already here")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "already exists" in row.verification_notes


def test_stale_partial_file(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    paths.deid_dir("sarin").mkdir(parents=True)
    (paths.deid_dir("sarin") / "UCD-FIL-001_video.partial.mp4").write_bytes(b"leftover")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "stale partial" in row.verification_notes


def test_ffmpeg_nonzero_exit_truncation_decoupled(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    huge_stderr = "x" * 1000

    def huge_failure(_in, _out):
        raise FFmpegError(stderr=huge_stderr, exit_code=234)

    _patch(monkeypatch, deid=huge_failure)
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert row.verification_notes.startswith("deid:")
    assert len(row.verification_notes.removeprefix("deid: ")) == 200

    entries = _audit_entries(paths)
    assert entries[0]["details"]["error_type"] == "FFmpegError"
    assert len(entries[0]["details"]["error"]) > 200
    assert huge_stderr in entries[0]["details"]["error"]


def test_empty_partial_after_deid(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch, deid=_empty_deid)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "output missing or empty" in row.verification_notes


def test_invalid_surgeon_uppercase_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    rc = deid_mod.handle(Namespace(surgeon="Sarin"), paths=paths)
    assert rc == 2
    assert not paths.state_csv.exists()
    assert not paths.audit_log.exists()


def test_invalid_surgeon_empty_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    rc = deid_mod.handle(Namespace(surgeon=""), paths=paths)
    assert rc == 2
    assert not paths.audit_log.exists()


def test_other_surgeons_concatenated_rows_untouched(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _make_concat_file(paths, "noren_20260103-080000.mp4")
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001", surgeon="sarin"),
        _manifest_row("UCD-FIL-099", surgeon="noren"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", concat_filename="sarin_20260101-080000.mp4"),
        _state_row("UCD-FIL-099", concat_filename="noren_20260103-080000.mp4"),
    )
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.deidentified
    assert rows["UCD-FIL-099"].stage == Stage.concatenated


def test_state_row_with_no_manifest_is_skipped(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_20260101-080000.mp4")
    _make_concat_file(paths, "sarin_20260102-080000.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", surgeon="sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", concat_filename="sarin_20260101-080000.mp4"),
        _state_row("UCD-FIL-002", concat_filename="sarin_20260102-080000.mp4"),
    )
    rc = deid_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.deidentified
    assert rows["UCD-FIL-002"].stage == Stage.concatenated


def test_case_happy_path_selects_only_named_case(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    for name in ["sarin_a.mp4", "sarin_b.mp4", "sarin_c.mp4"]:
        _make_concat_file(paths, name)
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001"),
        _manifest_row("UCD-FIL-002"),
        _manifest_row("UCD-FIL-003"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", concat_filename="sarin_a.mp4"),
        _state_row("UCD-FIL-002", concat_filename="sarin_b.mp4"),
        _state_row("UCD-FIL-003", concat_filename="sarin_c.mp4"),
    )
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-002"), paths=paths
    )
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.concatenated
    assert rows["UCD-FIL-002"].stage == Stage.deidentified
    assert rows["UCD-FIL-003"].stage == Stage.concatenated
    entries = _audit_entries(paths)
    assert len(entries) == 1
    assert entries[0]["case"] == "UCD-FIL-002"
    assert entries[0]["outcome"] == "success"


def test_case_bad_format_returns_2(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    before = paths.state_csv.read_bytes()
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="foo"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UCD-FIL-###" in err
    assert "'foo'" in err
    assert paths.state_csv.read_bytes() == before
    assert not paths.audit_log.exists()


def test_case_not_in_state_returns_2(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    before = paths.state_csv.read_bytes()
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-999"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UCD-FIL-999" in err
    assert "not found in state CSV" in err
    assert paths.state_csv.read_bytes() == before
    assert not paths.audit_log.exists()


def test_case_belongs_to_different_surgeon_returns_2(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", surgeon="noren"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UCD-FIL-001" in err
    assert "'noren'" in err
    assert "'sarin'" in err
    assert "no manifest" not in err
    assert not paths.audit_log.exists()


def test_case_has_no_manifest_entry_returns_2(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UCD-FIL-001" in err
    assert "no manifest entry" in err
    assert "belongs to surgeon=" not in err
    assert not paths.audit_log.exists()


@pytest.mark.parametrize("wrong_stage", [Stage.intake, Stage.deidentified, Stage.failed])
def test_case_at_wrong_stage_returns_2(tmp_path, capsys, wrong_stage):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001", stage=wrong_stage))
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UCD-FIL-001" in err
    assert f"'{wrong_stage.value}'" in err
    assert "concatenated" in err
    assert "status --case UCD-FIL-001" in err
    assert not paths.audit_log.exists()


def test_case_failure_inside_transaction_leaves_state_byte_identical(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch(monkeypatch)
    _make_concat_file(paths, "sarin_a.mp4")
    _make_concat_file(paths, "sarin_b.mp4")
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001"),
        _manifest_row("UCD-FIL-002"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", concat_filename="sarin_a.mp4"),
        _state_row("UCD-FIL-002", concat_filename="sarin_b.mp4"),
    )
    before_state = paths.state_csv.read_bytes()
    before_manifest = paths.manifest_csv.read_bytes()
    rc = deid_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-999"), paths=paths
    )
    assert rc == 2
    assert paths.state_csv.read_bytes() == before_state
    assert paths.manifest_csv.read_bytes() == before_manifest
    assert not paths.audit_log.exists()


def test_cli_bad_surgeon_returns_2_via_subprocess(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "pipeline", "deid", "--surgeon", "Sarin"],
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "PIPELINE_NAS_ROOT": str(tmp_path / "nas-not-yet"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "invalid surgeon name" in result.stderr
