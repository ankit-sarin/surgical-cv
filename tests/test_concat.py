import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pipeline import commands as commands_pkg  # noqa: F401
from pipeline.commands import concat as concat_mod
from pipeline.csv_io import CsvTable
from pipeline.ffmpeg import BdvFilenameError, CodecMismatchError, FFmpegError
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
        procedure_primary="Sigmoidectomy",
        approach="Robotic",
        indication="Diverticulitis",
        notes="",
    )
    base.update(kw)
    return CaseManifestRow(**base)


def _state_row(ucd_fil_id="UCD-FIL-001", raw_segments=None, **kw):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        raw_segments=raw_segments or ["capt0_20260101-080000.mp4"],
        concat_filename="",
        deid_filename="",
        stage=Stage.intake,
        concat_ts="",
        deid_ts="",
        verify_ts="",
        verification_notes="",
    )
    base.update(kw)
    return PipelineStateRow(**base)


def _seed_manifest(paths: NasPaths, *rows: CaseManifestRow):
    t = CsvTable(paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow)
    with t.transaction() as tx:
        for r in rows:
            tx.append(r)


def _seed_state(paths: NasPaths, *rows: PipelineStateRow):
    t = CsvTable(paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow)
    with t.transaction() as tx:
        for r in rows:
            tx.append(r)


def _state_rows(paths: NasPaths) -> dict[str, PipelineStateRow]:
    t = CsvTable(paths.state_csv, PIPELINE_STATE_COLUMNS, PipelineStateRow)
    return {r.ucd_fil_id: r for r in t.snapshot()}


def _make_segments(paths: NasPaths, surgeon: str, names: list[str]):
    raw_dir = paths.raw_dir(surgeon)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (raw_dir / name).write_bytes(b"x")
    return raw_dir, names


def _good_ffmpeg(_segments, output):
    Path(output).write_bytes(b"fake concat output")


def _bad_ffmpeg(_segments, _output):
    raise FFmpegError(stderr="invalid data", exit_code=2)


def _empty_ffmpeg(_segments, output):
    Path(output).write_bytes(b"")


def _patch_helpers(monkeypatch, *, ffmpeg=_good_ffmpeg, uniformity=lambda _p: None):
    monkeypatch.setattr(concat_mod, "ffmpeg_concat", ffmpeg)
    monkeypatch.setattr(concat_mod, "check_uniformity", uniformity)


def _audit_entries(paths: NasPaths) -> list[dict]:
    if not paths.audit_log.exists():
        return []
    return [json.loads(line) for line in paths.audit_log.read_text().splitlines()]


def test_no_intake_cases_returns_0_and_skips_audit(tmp_path, capsys, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No intake cases for surgeon=sarin" in out
    assert not paths.audit_log.exists()
    assert not paths.state_csv.exists()


def test_manifest_missing_entry_for_intake_row(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
        _state_row(
            "UCD-FIL-002",
            raw_segments=["capt0_20260101-090000.mp4"],
        ),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.concatenated
    assert rows["UCD-FIL-002"].stage == Stage.intake


def test_single_happy_path_case(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    raw_dir, segs = _make_segments(
        paths,
        "sarin",
        ["capt0_20260101-080000.mp4", "capt0_20260101-083000.mp4"],
    )
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(paths, _state_row("UCD-FIL-001", raw_segments=segs))

    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.concatenated
    assert row.concat_filename == "sarin_20260101-080000.mp4"
    assert row.concat_ts != ""
    output = paths.or_raw / "sarin_20260101-080000.mp4"
    assert output.exists()
    assert output.read_bytes() == b"fake concat output"
    for original_name in segs:
        assert not (raw_dir / original_name).exists()
        copied = raw_dir / original_name.replace(".mp4", "-copied.mp4")
        assert copied.exists()
    entries = _audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["command"] == "concat"
    assert e["outcome"] == "success"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["segments"] == 2
    assert e["details"]["output"] == "sarin_20260101-080000.mp4"


def test_two_cases_same_surgeon_both_succeed(tmp_path, monkeypatch, capsys):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    _make_segments(
        paths,
        "sarin",
        [
            "capt0_20260101-080000.mp4",
            "capt0_20260102-080000.mp4",
        ],
    )
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001", "sarin"),
        _manifest_row("UCD-FIL-002", "sarin"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
        _state_row("UCD-FIL-002", raw_segments=["capt0_20260102-080000.mp4"]),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.concatenated
    assert rows["UCD-FIL-002"].stage == Stage.concatenated
    out = capsys.readouterr().out
    assert "Processed 2 cases: 2 succeeded, 0 failed." in out
    entries = _audit_entries(paths)
    assert len([e for e in entries if e["outcome"] == "success"]) == 2


def test_success_plus_codec_mismatch_failure(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _make_segments(
        paths,
        "sarin",
        [
            "capt0_20260101-080000.mp4",
            "capt0_20260102-080000.mp4",
        ],
    )
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001", "sarin"),
        _manifest_row("UCD-FIL-002", "sarin"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
        _state_row("UCD-FIL-002", raw_segments=["capt0_20260102-080000.mp4"]),
    )

    seen = {"count": 0}

    def selective_uniformity(segs):
        seen["count"] += 1
        if seen["count"] == 2:
            raise CodecMismatchError(
                reference_path=segs[0],
                mismatched_path=segs[0],
                ref_signature=("h264", 1920, 1080, "30/1", "yuv420p"),
                mismatched_signature=("h264", 1920, 720, "30/1", "yuv420p"),
            )

    _patch_helpers(monkeypatch, uniformity=selective_uniformity)
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.concatenated
    assert rows["UCD-FIL-002"].stage == Stage.failed
    assert rows["UCD-FIL-002"].verification_notes.startswith("concat:")
    entries = _audit_entries(paths)
    by_case = {e["case"]: e for e in entries}
    assert by_case["UCD-FIL-001"]["outcome"] == "success"
    assert by_case["UCD-FIL-002"]["outcome"] == "failure"
    assert by_case["UCD-FIL-002"]["details"]["error_type"] == "CodecMismatchError"


def test_missing_segment_file(tmp_path, monkeypatch, capsys):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    paths.raw_dir("sarin").mkdir(parents=True)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    err = capsys.readouterr().err
    assert "FAILED" in err
    entries = _audit_entries(paths)
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["details"]["error_type"] == "FileNotFoundError"


def test_raw_dir_missing(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "raw directory not found" in row.verification_notes


def test_bdv_filename_error_on_first_segment(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    raw_dir = paths.raw_dir("sarin")
    raw_dir.mkdir(parents=True)
    (raw_dir / "weird.mp4").write_bytes(b"x")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["weird.mp4"]),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    entries = _audit_entries(paths)
    assert entries[0]["details"]["error_type"] == "BdvFilenameError"


def test_output_collision(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
    )
    (paths.or_raw / "sarin_20260101-080000.mp4").write_bytes(b"already here")
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "already exists" in row.verification_notes


def test_stale_partial_file(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
    )
    (paths.or_raw / "sarin_20260101-080000.partial.mp4").write_bytes(b"leftover")
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "stale partial" in row.verification_notes


def test_ffmpeg_failure(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch, ffmpeg=_bad_ffmpeg)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    entries = _audit_entries(paths)
    assert entries[0]["details"]["error_type"] == "FFmpegError"


def test_empty_partial_after_concat(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch, ffmpeg=_empty_ffmpeg)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "output missing or empty" in row.verification_notes


def test_segment_rename_failure_does_not_fail_case(tmp_path, monkeypatch, capsys):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    raw_dir, segs = _make_segments(
        paths, "sarin", ["capt0_20260101-080000.mp4"]
    )
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(paths, _state_row("UCD-FIL-001", raw_segments=segs))

    real_rename = os.rename

    def selective_rename(src, dst):
        if str(dst).endswith("-copied.mp4"):
            raise OSError("simulated rename failure")
        return real_rename(src, dst)

    monkeypatch.setattr("os.rename", selective_rename)
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.concatenated
    output = paths.or_raw / "sarin_20260101-080000.mp4"
    assert output.exists()
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "simulated rename failure" in err


def test_invalid_surgeon_uppercase_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    rc = concat_mod.handle(Namespace(surgeon="Sarin"), paths=paths)
    assert rc == 2
    assert not paths.state_csv.exists()
    assert not paths.audit_log.exists()


def test_invalid_surgeon_empty_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    rc = concat_mod.handle(Namespace(surgeon=""), paths=paths)
    assert rc == 2
    assert not paths.state_csv.exists()
    assert not paths.audit_log.exists()


def test_other_surgeons_intake_rows_untouched(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_helpers(monkeypatch)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    paths.raw_dir("noren").mkdir(parents=True)
    (paths.raw_dir("noren") / "capt0_20260103-080000.mp4").write_bytes(b"x")

    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001", "sarin"),
        _manifest_row("UCD-FIL-099", "noren"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
        _state_row("UCD-FIL-099", raw_segments=["capt0_20260103-080000.mp4"]),
    )
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.concatenated
    assert rows["UCD-FIL-099"].stage == Stage.intake


def test_partial_filename_keeps_mp4_extension(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    raw_dir, segs = _make_segments(
        paths, "sarin", ["capt0_20260101-080000.mp4"]
    )
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(paths, _state_row("UCD-FIL-001", raw_segments=segs))

    captured = {}

    def capturing_ffmpeg(segments, output):
        captured["output"] = Path(output)
        Path(output).write_bytes(b"fake")

    _patch_helpers(monkeypatch, ffmpeg=capturing_ffmpeg)
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 0

    out_path = captured["output"]
    assert out_path.name.endswith(".partial.mp4")
    assert not out_path.name.endswith(".partial")

    final_output = paths.or_raw / "sarin_20260101-080000.mp4"
    assert final_output.exists()
    assert final_output.name.endswith(".mp4")
    assert not final_output.name.endswith(".partial.mp4")
    assert not out_path.exists(), "partial path should have been renamed away"


def test_error_truncation_decoupled_between_csv_and_audit(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _make_segments(paths, "sarin", ["capt0_20260101-080000.mp4"])
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", "sarin"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", raw_segments=["capt0_20260101-080000.mp4"]),
    )

    huge_stderr = "x" * 1000

    def huge_failure(_segments, _output):
        raise FFmpegError(stderr=huge_stderr, exit_code=234)

    _patch_helpers(monkeypatch, ffmpeg=huge_failure)
    rc = concat_mod.handle(Namespace(surgeon="sarin"), paths=paths)
    assert rc == 1

    row = _state_rows(paths)["UCD-FIL-001"]
    full_str = str(FFmpegError(stderr=huge_stderr, exit_code=234))
    expected_notes = "concat: " + full_str[:200]
    assert row.verification_notes == expected_notes
    assert len(row.verification_notes.removeprefix("concat: ")) == 200

    entries = _audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["details"]["error_type"] == "FFmpegError"
    assert e["details"]["error"] == full_str
    assert len(e["details"]["error"]) > 200
    assert huge_stderr in e["details"]["error"]


def test_cli_bad_surgeon_returns_2_via_subprocess(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "pipeline", "concat", "--surgeon", "Sarin"],
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
