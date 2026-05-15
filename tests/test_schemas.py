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
        procedure_primary="Sigmoidectomy",
        procedure_additional=[],
        approach="Robotic",
        conversion_target="",
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
    assert len(CASE_MANIFEST_COLUMNS) == 10
    assert len(PIPELINE_STATE_COLUMNS) == 10


def test_manifest_columns_include_spec_j_schema():
    """The Spec J schema extension: procedure_primary replaces procedure_name,
    procedure_additional + conversion_target are new columns."""
    assert "procedure_primary" in CASE_MANIFEST_COLUMNS
    assert "procedure_additional" in CASE_MANIFEST_COLUMNS
    assert "conversion_target" in CASE_MANIFEST_COLUMNS
    assert "procedure_name" not in CASE_MANIFEST_COLUMNS


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
        procedure_primary="LAR",
        approach="Laparoscopic",
        indication="Cancer",
    )
    assert r2.notes == ""
    assert r2.procedure_additional == []
    assert r2.conversion_target == ""


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
    for field in ("or_room", "procedure_primary", "approach", "indication"):
        with pytest.raises(ValidationError):
            CaseManifestRow(**_valid_manifest_kwargs(**{field: ""}))


def test_case_manifest_row_round_trip_with_empty_notes():
    r = CaseManifestRow(**_valid_manifest_kwargs(notes=""))
    d = r.to_csv_dict()
    assert d["notes"] == ""
    # procedure_additional round-trips list[] ↔ "" — the disk encoding
    # differs from the in-memory attribute, so compare directly.
    assert d["procedure_additional"] == ""
    for col in CASE_MANIFEST_COLUMNS:
        if col == "procedure_additional":
            continue
        assert d[col] == getattr(r, col)
    assert CaseManifestRow.from_csv_dict(d) == r


def test_case_manifest_row_round_trip_with_notes_text():
    r = CaseManifestRow(**_valid_manifest_kwargs(notes="redo case"))
    d = r.to_csv_dict()
    assert d["notes"] == "redo case"
    assert CaseManifestRow.from_csv_dict(d) == r


def test_procedure_additional_empty_list_serializes_as_empty_string():
    """Empty list collapses to "" on disk (readability for the unaffected
    majority of rows)."""
    r = CaseManifestRow(**_valid_manifest_kwargs(procedure_additional=[]))
    d = r.to_csv_dict()
    assert d["procedure_additional"] == ""


def test_procedure_additional_non_empty_serializes_as_json_array():
    r = CaseManifestRow(
        **_valid_manifest_kwargs(procedure_additional=["TAMIS"])
    )
    d = r.to_csv_dict()
    assert d["procedure_additional"] == '["TAMIS"]'


def test_procedure_additional_round_trip_empty():
    r = CaseManifestRow(**_valid_manifest_kwargs(procedure_additional=[]))
    d = r.to_csv_dict()
    back = CaseManifestRow.from_csv_dict(d)
    assert back == r
    assert back.procedure_additional == []


def test_procedure_additional_round_trip_with_values():
    r = CaseManifestRow(
        **_valid_manifest_kwargs(
            procedure_additional=["TAMIS", "Diverting loop ileostomy"]
        )
    )
    d = r.to_csv_dict()
    back = CaseManifestRow.from_csv_dict(d)
    assert back == r
    assert back.procedure_additional == [
        "TAMIS", "Diverting loop ileostomy",
    ]


def test_procedure_additional_from_csv_empty_string_yields_empty_list():
    d = {col: "" for col in CASE_MANIFEST_COLUMNS}
    d.update(
        {
            "ucd_fil_id": "UCD-FIL-001",
            "surgeon": "sarin",
            "case_year": "2026",
            "or_room": "OR 4",
            "procedure_primary": "Sigmoidectomy",
            "approach": "Robotic",
            "indication": "Diverticulitis",
        }
    )
    row = CaseManifestRow.from_csv_dict(d)
    assert row.procedure_additional == []


def test_procedure_additional_invalid_json_raises_at_csv_read():
    d = {col: "" for col in CASE_MANIFEST_COLUMNS}
    d.update(
        {
            "ucd_fil_id": "UCD-FIL-001",
            "surgeon": "sarin",
            "case_year": "2026",
            "or_room": "OR 4",
            "procedure_primary": "Sigmoidectomy",
            "procedure_additional": "not_json{{{",
            "approach": "Robotic",
            "indication": "Diverticulitis",
        }
    )
    with pytest.raises(ValueError, match="procedure_additional"):
        CaseManifestRow.from_csv_dict(d)


def test_procedure_additional_non_array_root_raises_at_csv_read():
    d = {col: "" for col in CASE_MANIFEST_COLUMNS}
    d.update(
        {
            "ucd_fil_id": "UCD-FIL-001",
            "surgeon": "sarin",
            "case_year": "2026",
            "or_room": "OR 4",
            "procedure_primary": "Sigmoidectomy",
            "procedure_additional": '{"not": "a list"}',
            "approach": "Robotic",
            "indication": "Diverticulitis",
        }
    )
    with pytest.raises(ValueError, match="JSON array"):
        CaseManifestRow.from_csv_dict(d)


def test_procedure_additional_rejects_empty_string_element():
    with pytest.raises(ValidationError):
        CaseManifestRow(
            **_valid_manifest_kwargs(procedure_additional=["Valid", ""])
        )


def test_conversion_target_default_empty():
    r = CaseManifestRow(**_valid_manifest_kwargs())
    assert r.conversion_target == ""


def test_conversion_target_round_trip():
    r = CaseManifestRow(
        **_valid_manifest_kwargs(approach="Robotic", conversion_target="Open")
    )
    d = r.to_csv_dict()
    assert d["conversion_target"] == "Open"
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
