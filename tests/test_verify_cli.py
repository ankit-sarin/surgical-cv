import json
from argparse import Namespace
from pathlib import Path

import pytest

from pipeline import diagnostician as diag_mod
from pipeline.commands import verify as verify_mod
from pipeline.csv_io import CsvTable
from pipeline.diagnostician import DiagnosticianInfraError
from pipeline.paths import NasPaths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    DiagnosticianVerdict,
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
    stage=Stage.deidentified,
    deid_filename="UCD-FIL-001_video.mp4",
    **kw,
):
    base = dict(
        ucd_fil_id=ucd_fil_id,
        raw_segments=["capt0_20260101-080000.mp4"],
        concat_filename="sarin_20260101-080000.mp4",
        deid_filename=deid_filename,
        stage=stage,
        concat_ts="2026-05-12T10:00:00+00:00",
        deid_ts="2026-05-12T11:00:00+00:00",
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


def _make_deid_file(paths: NasPaths, surgeon: str, filename: str) -> Path:
    d = paths.deid_dir(surgeon)
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_bytes(b"fake deid bytes")
    return p


def _audit_entries(paths: NasPaths) -> list[dict]:
    if not paths.audit_log.exists():
        return []
    return [json.loads(line) for line in paths.audit_log.read_text().splitlines()]


_PASS_VERDICT = DiagnosticianVerdict(
    verdict="pass",
    reason="Zero audio streams, encoder=Lavf60, h264 codec confirmed.",
    evidence=["audio streams=0", "encoder=Lavf60.16.100", "codec=h264"],
)

_FAIL_VERDICT = DiagnosticianVerdict(
    verdict="fail",
    reason="Format-level title tag leaks patient name.",
    evidence=["format.tags.title=Smith, John"],
)


def _stub_evidence(audio_count=0, format_tags=None, exiftool=None):
    streams = [{"codec_type": "video", "codec_name": "h264"}]
    for _ in range(audio_count):
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {
        "ffprobe": {
            "streams": streams,
            "format": {"tags": format_tags or {"encoder": "Lavf60.16.100"}},
        },
        "exiftool": exiftool or {},
        "ffmpeg_stderr": "Stream #0:0: Video: h264",
    }


def _patch_clean(monkeypatch, *, verdict=_PASS_VERDICT, evidence=None):
    monkeypatch.setattr(
        verify_mod, "collect_evidence", lambda p: evidence or _stub_evidence()
    )
    monkeypatch.setattr(verify_mod, "diagnose", lambda ev: verdict)


def test_invalid_surgeon_uppercase_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    rc = verify_mod.handle(Namespace(surgeon="Sarin", case=None), paths=paths)
    assert rc == 2
    assert not paths.audit_log.exists()


def test_case_bad_format_returns_2(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    before = paths.state_csv.read_bytes()
    rc = verify_mod.handle(
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
    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-999"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UCD-FIL-999" in err
    assert "not found in state CSV" in err
    assert paths.state_csv.read_bytes() == before
    assert not paths.audit_log.exists()


def test_case_wrong_surgeon_returns_2(tmp_path, capsys):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", surgeon="noren"))
    _seed_state(paths, _state_row("UCD-FIL-001"))
    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "'noren'" in err
    assert "'sarin'" in err
    assert not paths.audit_log.exists()


@pytest.mark.parametrize(
    "wrong_stage", [Stage.intake, Stage.concatenated, Stage.verified]
)
def test_case_wrong_stage_returns_2(tmp_path, capsys, wrong_stage):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001", stage=wrong_stage))
    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert f"'{wrong_stage.value}'" in err
    assert "deidentified" in err
    assert "failed" in err
    assert not paths.audit_log.exists()


def test_mocked_pass_transitions_deidentified_to_verified(
    tmp_path, monkeypatch
):
    paths = _make_paths(tmp_path)
    _patch_clean(monkeypatch)
    _make_deid_file(paths, "sarin", "UCD-FIL-001_video.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 0
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.verified
    assert row.verify_ts != ""
    assert row.verification_notes.startswith("verified:")

    entries = _audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["command"] == "verify"
    assert e["outcome"] == "success"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["preflight_passed"] is True
    assert e["details"]["verdict"]["verdict"] == "pass"


def test_mocked_diagnostician_fail_transitions_to_failed(
    tmp_path, monkeypatch
):
    paths = _make_paths(tmp_path)
    _patch_clean(monkeypatch, verdict=_FAIL_VERDICT)
    _make_deid_file(paths, "sarin", "UCD-FIL-001_video.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert row.verify_ts != ""
    assert row.verification_notes.startswith("diagnostician:")
    assert "patient name" in row.verification_notes

    entries = _audit_entries(paths)
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["details"]["failure_kind"] == "diagnostician"
    assert entries[0]["details"]["verdict"]["verdict"] == "fail"


def test_preflight_audio_failure_skips_diagnostician(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    diagnose_called = {"n": 0}

    def fake_diagnose(ev):
        diagnose_called["n"] += 1
        return _PASS_VERDICT

    monkeypatch.setattr(
        verify_mod, "collect_evidence", lambda p: _stub_evidence(audio_count=1)
    )
    monkeypatch.setattr(verify_mod, "diagnose", fake_diagnose)
    _make_deid_file(paths, "sarin", "UCD-FIL-001_video.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 1
    assert diagnose_called["n"] == 0
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert row.verification_notes.startswith("preflight PF1")

    entries = _audit_entries(paths)
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["details"]["failure_kind"] == "preflight"
    assert entries[0]["details"]["check_id"] == "PF1"


def test_retry_path_from_failed_to_verified(tmp_path, monkeypatch):
    """A case at stage=failed should be eligible for re-verification."""
    paths = _make_paths(tmp_path)
    _patch_clean(monkeypatch)
    _make_deid_file(paths, "sarin", "UCD-FIL-001_video.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(
        paths,
        _state_row(
            "UCD-FIL-001",
            stage=Stage.failed,
            verification_notes="previous verify failure",
        ),
    )

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 0
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.verified
    assert row.verify_ts != ""


def test_infra_error_malformed_leaves_state_unchanged(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)

    def fake_diagnose(ev):
        raise DiagnosticianInfraError(
            reason="malformed_output", raw_outputs=["junk1", "junk2"]
        )

    monkeypatch.setattr(verify_mod, "collect_evidence", lambda p: _stub_evidence())
    monkeypatch.setattr(verify_mod, "diagnose", fake_diagnose)
    _make_deid_file(paths, "sarin", "UCD-FIL-001_video.mp4")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 2
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.deidentified  # unchanged
    assert row.verify_ts == ""  # NOT written
    assert row.verification_notes == ""

    entries = _audit_entries(paths)
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["details"]["failure_kind"] == "infra"
    assert entries[0]["details"]["infra_reason"] == "malformed_output"
    assert entries[0]["details"]["raw_outputs"] == ["junk1", "junk2"]


def test_infra_error_ollama_unavailable_aborts_batch(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    call_count = {"n": 0}

    def fake_diagnose(ev):
        call_count["n"] += 1
        raise DiagnosticianInfraError(
            reason="ollama_unavailable", error="connection refused"
        )

    monkeypatch.setattr(verify_mod, "collect_evidence", lambda p: _stub_evidence())
    monkeypatch.setattr(verify_mod, "diagnose", fake_diagnose)
    for case in ("UCD-FIL-001", "UCD-FIL-002"):
        _make_deid_file(paths, "sarin", f"{case}_video.mp4")
    _seed_manifest(
        paths, _manifest_row("UCD-FIL-001"), _manifest_row("UCD-FIL-002")
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", deid_filename="UCD-FIL-001_video.mp4"),
        _state_row("UCD-FIL-002", deid_filename="UCD-FIL-002_video.mp4"),
    )

    rc = verify_mod.handle(Namespace(surgeon="sarin", case=None), paths=paths)
    assert rc == 2
    # Batch aborted after first failure — only one diagnose call.
    assert call_count["n"] == 1

    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.deidentified
    assert rows["UCD-FIL-002"].stage == Stage.deidentified

    entries = _audit_entries(paths)
    assert len(entries) == 1
    assert entries[0]["details"]["infra_reason"] == "ollama_unavailable"


def test_missing_deid_artifact_transitions_to_failed(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    # collect_evidence is never called because the file-existence check
    # happens before it. We still patch diagnose just in case.
    monkeypatch.setattr(verify_mod, "diagnose", lambda ev: _PASS_VERDICT)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", deid_filename="UCD-FIL-001_video.mp4"),
    )

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "deid artifact not found" in row.verification_notes


def test_batch_mode_processes_only_matching_surgeon(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    _patch_clean(monkeypatch)
    _make_deid_file(paths, "sarin", "UCD-FIL-001_video.mp4")
    _make_deid_file(paths, "noren", "UCD-FIL-099_video.mp4")
    _seed_manifest(
        paths,
        _manifest_row("UCD-FIL-001", surgeon="sarin"),
        _manifest_row("UCD-FIL-099", surgeon="noren"),
    )
    _seed_state(
        paths,
        _state_row("UCD-FIL-001", deid_filename="UCD-FIL-001_video.mp4"),
        _state_row("UCD-FIL-099", deid_filename="UCD-FIL-099_video.mp4"),
    )

    rc = verify_mod.handle(Namespace(surgeon="sarin", case=None), paths=paths)
    assert rc == 0
    rows = _state_rows(paths)
    assert rows["UCD-FIL-001"].stage == Stage.verified
    assert rows["UCD-FIL-099"].stage == Stage.deidentified


def test_empty_deid_filename_fails_with_clear_error(tmp_path, monkeypatch):
    paths = _make_paths(tmp_path)
    monkeypatch.setattr(verify_mod, "diagnose", lambda ev: _PASS_VERDICT)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001", deid_filename=""))

    rc = verify_mod.handle(
        Namespace(surgeon="sarin", case="UCD-FIL-001"), paths=paths
    )
    assert rc == 1
    row = _state_rows(paths)["UCD-FIL-001"]
    assert row.stage == Stage.failed
    assert "deid_filename is empty" in row.verification_notes
