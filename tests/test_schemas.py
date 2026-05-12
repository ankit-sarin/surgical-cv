import pytest
from pydantic import ValidationError

from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    DiagnosticianVerdict,
    PipelineStateRow,
    Stage,
    is_valid_transition,
)


def _valid_manifest_kwargs(**overrides):
    base = dict(
        ucd_fil_id="UCD-FIL-001",
        surgeon="sarin",
        case_year="2026",
        or_room="OR4",
        procedure_name="Sigmoidectomy",
        approach="Robotic",
        indication="Diverticulitis",
        notes="",
    )
    base.update(overrides)
    return base


def _valid_state_kwargs(**overrides):
    base = dict(
        ucd_fil_id="UCD-FIL-001",
        raw_segments=["a.mp4", "b.mp4"],
        concat_filename="",
        deid_filename="",
        stage=Stage.intake,
        concat_ts="",
        deid_ts="",
        verify_ts="",
        verification_notes="",
    )
    base.update(overrides)
    return base


def test_columns_have_expected_lengths():
    assert len(CASE_MANIFEST_COLUMNS) == 8
    assert len(PIPELINE_STATE_COLUMNS) == 9


def test_stage_values():
    assert Stage.intake.value == "intake"
    assert Stage.concatenated.value == "concatenated"
    assert Stage.deidentified.value == "deidentified"
    assert Stage.verified.value == "verified"
    assert Stage.failed.value == "failed"
    assert list(Stage) == [
        Stage.intake,
        Stage.concatenated,
        Stage.deidentified,
        Stage.verified,
        Stage.failed,
    ]


def test_is_valid_transition_happy_paths():
    assert is_valid_transition(Stage.intake, Stage.concatenated)
    assert is_valid_transition(Stage.concatenated, Stage.deidentified)
    assert is_valid_transition(Stage.deidentified, Stage.verified)
    assert is_valid_transition(Stage.deidentified, Stage.failed)
    assert is_valid_transition(Stage.concatenated, Stage.failed)
    assert is_valid_transition(Stage.intake, Stage.failed)
    assert is_valid_transition(Stage.failed, Stage.deidentified)


def test_is_valid_transition_rejects_terminal_and_self():
    assert not is_valid_transition(Stage.verified, Stage.failed)
    assert not is_valid_transition(Stage.verified, Stage.deidentified)
    for s in Stage:
        assert not is_valid_transition(s, s), f"self-transition {s} should be invalid"
    assert not is_valid_transition(Stage.intake, Stage.deidentified)
    assert not is_valid_transition(Stage.failed, Stage.verified)


def test_case_manifest_row_construct_and_defaults():
    r = CaseManifestRow(**_valid_manifest_kwargs(notes=""))
    assert r.notes == ""
    r2 = CaseManifestRow(
        ucd_fil_id="UCD-FIL-002",
        surgeon="miller",
        case_year="2025",
        or_room="OR1",
        procedure_name="LAR",
        approach="Laparoscopic",
        indication="Cancer",
    )
    assert r2.notes == ""


def test_case_manifest_row_invalid_ucd_fil_id():
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(ucd_fil_id="UCD-001"))
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(ucd_fil_id="UCD-FIL-1"))


def test_case_manifest_row_invalid_case_year():
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(case_year="26"))
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(case_year="20260"))
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(case_year="abcd"))


def test_case_manifest_row_surgeon_rules():
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(surgeon="Sarin"))
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(surgeon="sa rin"))
    with pytest.raises(ValidationError):
        CaseManifestRow(**_valid_manifest_kwargs(surgeon=""))


def test_case_manifest_row_other_non_empty_fields():
    for field in ("or_room", "procedure_name", "approach", "indication"):
        with pytest.raises(ValidationError):
            CaseManifestRow(**_valid_manifest_kwargs(**{field: ""}))


def test_case_manifest_row_round_trip_with_empty_notes():
    r = CaseManifestRow(**_valid_manifest_kwargs(notes=""))
    d = r.to_csv_dict()
    assert d["notes"] == ""
    assert d == {col: getattr(r, col) for col in CASE_MANIFEST_COLUMNS}
    assert CaseManifestRow.from_csv_dict(d) == r


def test_case_manifest_row_round_trip_with_notes_text():
    r = CaseManifestRow(**_valid_manifest_kwargs(notes="redo case"))
    d = r.to_csv_dict()
    assert d["notes"] == "redo case"
    assert CaseManifestRow.from_csv_dict(d) == r


def test_pipeline_state_row_construct():
    r = PipelineStateRow(**_valid_state_kwargs())
    assert r.stage == Stage.intake
    assert r.raw_segments == ["a.mp4", "b.mp4"]


def test_pipeline_state_row_empty_segments_rejected():
    with pytest.raises(ValidationError):
        PipelineStateRow(**_valid_state_kwargs(raw_segments=[]))


def test_pipeline_state_row_segment_with_pipe_rejected():
    with pytest.raises(ValidationError):
        PipelineStateRow(**_valid_state_kwargs(raw_segments=["a|b.mp4"]))


def test_pipeline_state_row_round_trip():
    r = PipelineStateRow(**_valid_state_kwargs())
    d = r.to_csv_dict()
    assert d["raw_segments"] == "a.mp4|b.mp4"
    assert d["stage"] == "intake"
    back = PipelineStateRow.from_csv_dict(d)
    assert back == r


def test_pipeline_state_row_round_trip_with_timestamps():
    r = PipelineStateRow(
        **_valid_state_kwargs(
            stage=Stage.verified,
            concat_filename="UCD-FIL-001_raw.mp4",
            deid_filename="UCD-FIL-001_video.mp4",
            concat_ts="2026-05-12T09:30:00",
            deid_ts="2026-05-12T10:15:00",
            verify_ts="2026-05-12T10:45:00",
            verification_notes="all good",
        )
    )
    d = r.to_csv_dict()
    back = PipelineStateRow.from_csv_dict(d)
    assert back == r


def test_pipeline_state_row_invalid_iso_timestamp():
    with pytest.raises(ValidationError):
        PipelineStateRow(**_valid_state_kwargs(concat_ts="not-a-ts"))


def test_pipeline_state_row_empty_timestamps_allowed():
    r = PipelineStateRow(**_valid_state_kwargs(concat_ts="", deid_ts="", verify_ts=""))
    assert r.concat_ts == ""


def test_pipeline_state_row_stage_accepted_as_string():
    r = PipelineStateRow.from_csv_dict(
        {
            "ucd_fil_id": "UCD-FIL-001",
            "raw_segments": "a.mp4|b.mp4",
            "concat_filename": "",
            "deid_filename": "",
            "stage": "concatenated",
            "concat_ts": "",
            "deid_ts": "",
            "verify_ts": "",
            "verification_notes": "",
        }
    )
    assert r.stage == Stage.concatenated


def test_diagnostician_verdict_valid_pass_and_fail():
    v1 = DiagnosticianVerdict(verdict="pass", reason="looks clean", evidence=["no audio"])
    assert v1.verdict == "pass"
    v2 = DiagnosticianVerdict(verdict="fail", reason="audio detected", evidence=["audio at 00:30"])
    assert v2.verdict == "fail"


def test_diagnostician_verdict_invalid_verdict():
    with pytest.raises(ValidationError):
        DiagnosticianVerdict(verdict="maybe", reason="ok", evidence=[])


def test_diagnostician_verdict_extra_forbidden():
    with pytest.raises(ValidationError):
        DiagnosticianVerdict(verdict="pass", reason="ok", evidence=[], extra_field=1)


def test_diagnostician_verdict_reason_length_and_emptiness():
    with pytest.raises(ValidationError):
        DiagnosticianVerdict(verdict="pass", reason="", evidence=[])
    with pytest.raises(ValidationError):
        DiagnosticianVerdict(verdict="pass", reason="x" * 201, evidence=[])


def test_diagnostician_verdict_evidence_constraints():
    with pytest.raises(ValidationError):
        DiagnosticianVerdict(verdict="pass", reason="ok", evidence=["a", ""])
    with pytest.raises(ValidationError):
        DiagnosticianVerdict(
            verdict="pass",
            reason="ok",
            evidence=[f"item{i}" for i in range(11)],
        )
    v = DiagnosticianVerdict(
        verdict="pass",
        reason="ok",
        evidence=[f"item{i}" for i in range(10)],
    )
    assert len(v.evidence) == 10
