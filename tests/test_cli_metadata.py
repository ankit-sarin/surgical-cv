import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pipeline.csv_io import CsvTable
from pipeline.paths import NasPaths
from pipeline.schemas import (
    CASE_MANIFEST_COLUMNS,
    PIPELINE_STATE_COLUMNS,
    CaseManifestRow,
    PipelineStateRow,
    Stage,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_EDITABLE_FIELDS = (
    "case_year",
    "or_room",
    "procedure_primary",
    "procedure_additional",
    "approach",
    "conversion_target",
    "indication",
    "notes",
)


def run(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "pipeline", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
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


def _manifest_row(
    ucd_fil_id="UCD-FIL-001",
    surgeon="sarin",
    case_year="2026",
    or_room="OR4",
    procedure_primary="Sigmoidectomy",
    procedure_additional=None,
    approach="Robotic",
    conversion_target="",
    indication="Diverticulitis",
    notes="",
):
    return CaseManifestRow(
        ucd_fil_id=ucd_fil_id,
        surgeon=surgeon,
        case_year=case_year,
        or_room=or_room,
        procedure_primary=procedure_primary,
        procedure_additional=procedure_additional or [],
        approach=approach,
        conversion_target=conversion_target,
        indication=indication,
        notes=notes,
    )


def _state_row(
    ucd_fil_id="UCD-FIL-001",
    stage=Stage.verified,
    verify_ts="2026-05-13T01:34:23+00:00",
    deid_ts="2026-05-12T14:21:52+00:00",
    concat_ts="2026-05-12T13:26:34+00:00",
    deid_filename="UCD-FIL-001_video.mp4",
    concat_filename="sarin_20260101-080000.mp4",
    verification_notes="verified: all checks passed",
):
    return PipelineStateRow(
        ucd_fil_id=ucd_fil_id,
        raw_segments=["capt0_20260101-080000.mp4"],
        concat_filename=concat_filename,
        deid_filename=deid_filename,
        stage=stage,
        concat_ts=concat_ts,
        deid_ts=deid_ts,
        verify_ts=verify_ts,
        verification_notes=verification_notes,
    )


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


def _read_manifest(paths: NasPaths) -> dict[str, CaseManifestRow]:
    t = CsvTable(paths.manifest_csv, CASE_MANIFEST_COLUMNS, CaseManifestRow)
    return {r.ucd_fil_id: r for r in t.snapshot()}


def _read_audit_entries(paths: NasPaths) -> list[dict]:
    if not paths.audit_log.exists():
        return []
    return [json.loads(line) for line in paths.audit_log.read_text().splitlines()]


def _env(
    paths: NasPaths,
    vocab_dir: Path | None = None,
    picklist_dir: Path | None = None,
) -> dict:
    env = {**os.environ, "PIPELINE_NAS_ROOT": str(paths.root)}
    if vocab_dir is not None and picklist_dir is None:
        picklist_dir = vocab_dir / "picklists"
    if picklist_dir is not None:
        env["PIPELINE_PICKLIST_DIR"] = str(picklist_dir)
    return env


_DEFAULT_PROCEDURES = [
    "Right hemicolectomy",
    "Sigmoidectomy",
    "Other",
]
_DEFAULT_APPROACHES = ["Open", "Laparoscopic", "Robotic", "Hybrid"]
_DEFAULT_INDICATIONS = ["Colorectal cancer", "Diverticulitis", "Other"]
_DEFAULT_CASE_YEARS = ["2025", "2026", "2027"]


def _seed_picklist(
    picklist_dir: Path,
    field: str,
    specialty: str | None,
    values: list[str] | str | None,
) -> Path:
    """Write a structured picklist file derived from a flat list of values.

    None → omit the file (missing-file test).
    str  → write the raw payload (malformed-JSON test).
    list → wrap into the {field, specialty, values[]} structured format.
    """
    picklist_dir.mkdir(parents=True, exist_ok=True)
    if values is None:
        return picklist_dir
    name = f"{field}_{specialty}.json" if specialty else f"{field}.json"
    target = picklist_dir / name
    if isinstance(values, str):
        target.write_text(values)
        return picklist_dir
    structured = {
        "field": field,
        "specialty": specialty,
        "values": [
            {"value": v, "display_label": v, "sort_order": (i + 1) * 10}
            for i, v in enumerate(values)
        ],
    }
    target.write_text(json.dumps(structured))
    return picklist_dir


def _seed_full_vocab(vocab_dir: Path) -> Path:
    vocab_dir.mkdir(parents=True, exist_ok=True)
    pdir = vocab_dir / "picklists"
    _seed_picklist(pdir, "procedure", "colorectal", _DEFAULT_PROCEDURES)
    _seed_picklist(pdir, "approach", None, _DEFAULT_APPROACHES)
    _seed_picklist(pdir, "indication", "colorectal", _DEFAULT_INDICATIONS)
    _seed_picklist(pdir, "case_year", None, _DEFAULT_CASE_YEARS)
    return vocab_dir


# ----- argparse / stub tests -----


def test_metadata_help_lists_six_editable_fields():
    result = run("metadata", "--help")
    assert result.returncode == 0
    for field in _EDITABLE_FIELDS:
        assert field in result.stdout, (
            f"--help text missing editable field {field!r}\n"
            f"stdout was:\n{result.stdout}"
        )


def test_metadata_confirm_without_edit_rejected_by_argparse():
    result = run("metadata", "UCD-FIL-001", "--confirm")
    assert result.returncode != 0
    err = result.stderr
    assert "--confirm" in err or "--edit" in err


def test_metadata_edit_with_field_not_in_choices_rejected():
    result = run(
        "metadata", "UCD-FIL-001", "--edit", "ucd_fil_id", "999", "--confirm"
    )
    assert result.returncode != 0
    err = result.stderr
    assert "--edit" in err
    assert "ucd_fil_id" in err


def test_metadata_show_and_edit_are_mutually_exclusive():
    result = run("metadata", "UCD-FIL-001", "--show", "--edit", "notes", "x")
    assert result.returncode != 0
    err = result.stderr
    assert "--show" in err or "--edit" in err or "not allowed" in err


# ----- --show success and error paths -----


def test_show_success_displays_all_eight_field_labels_and_stage(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", notes="some surgeon note"))
    _seed_state(paths, _state_row("UCD-FIL-001", stage=Stage.verified))

    result = run("metadata", "UCD-FIL-001", env=_env(paths))
    assert result.returncode == 0
    out = result.stdout

    for label in (
        "Case:",
        "Surgeon:",
        "Case year:",
        "OR room:",
        "Procedure:",
        "Approach:",
        "Indication:",
        "Notes:",
    ):
        assert label in out, f"missing label {label!r}\n{out}"

    assert "UCD-FIL-001" in out
    assert "sarin" in out
    assert "Sigmoidectomy" in out
    assert "Pipeline stage: verified" in out
    assert "verify_ts: 2026-05-13T01:34:23+00:00" in out


def test_show_case_not_in_manifest_exits_1(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))

    result = run("metadata", "UCD-FIL-999", env=_env(paths))
    assert result.returncode == 1
    assert "error: case not found in manifest: UCD-FIL-999" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "bad_case_id", ["FOOBAR", "UCD-FIL-99", "UCD-FIL-1000", "ucd-fil-001", ""]
)
def test_show_bad_case_format_exits_1(tmp_path, bad_case_id):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))

    # Empty string is a tricky positional — argparse will either accept it
    # and our code rejects it, or argparse rejects it first. Either way the
    # exit code is non-zero. For the other cases we expect our specific
    # error message.
    result = run("metadata", bad_case_id, env=_env(paths))
    assert result.returncode != 0
    if bad_case_id:
        assert f"error: invalid case ID format: {bad_case_id}" in result.stderr


def test_show_case_in_manifest_missing_from_state_renders_gracefully(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    # No state seed.

    result = run("metadata", "UCD-FIL-001", env=_env(paths))
    assert result.returncode == 0
    assert "Pipeline stage: <not in state>" in result.stdout
    assert "UCD-FIL-001" in result.stdout


def test_show_empty_notes_renders_as_empty_marker(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", notes=""))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    result = run("metadata", "UCD-FIL-001", env=_env(paths))
    assert result.returncode == 0
    assert "(empty)" in result.stdout
    # Sanity: there should not be a Notes line with no value.
    for line in result.stdout.splitlines():
        if line.startswith("Notes:"):
            assert "(empty)" in line


def test_show_non_empty_notes_renders_value_not_empty_marker(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", notes="airway issues"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    result = run("metadata", "UCD-FIL-001", env=_env(paths))
    assert result.returncode == 0
    assert "airway issues" in result.stdout
    assert "(empty)" not in result.stdout


def test_default_no_flags_byte_identical_to_explicit_show(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001", notes="x"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    bare = run("metadata", "UCD-FIL-001", env=_env(paths))
    explicit = run("metadata", "UCD-FIL-001", "--show", env=_env(paths))
    assert bare.returncode == 0
    assert explicit.returncode == 0
    assert bare.stdout == explicit.stdout
    assert bare.stderr == explicit.stderr


def test_show_writes_nothing_to_pipeline_log(tmp_path):
    paths = _make_paths(tmp_path)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    _seed_state(paths, _state_row("UCD-FIL-001"))

    # Success path
    assert run("metadata", "UCD-FIL-001", env=_env(paths)).returncode == 0
    # Case-not-found error path
    assert run("metadata", "UCD-FIL-999", env=_env(paths)).returncode == 1
    # Bad-format error path
    assert run("metadata", "FOOBAR", env=_env(paths)).returncode == 1

    assert not paths.audit_log.exists(), (
        "pipeline.log should not be created by any --show invocation"
    )


# ----- --edit dry-run tests -----


def _setup_dry_run(tmp_path: Path) -> tuple[NasPaths, Path, bytes]:
    """Seed manifest + state + full vocab and snapshot manifest bytes."""
    paths = _make_paths(tmp_path)
    vocab_dir = _seed_full_vocab(tmp_path / "vocab")
    _seed_manifest(
        paths,
        _manifest_row(
            "UCD-FIL-001",
            case_year="2026",
            or_room="OR4",
            procedure_primary="Other",
            approach="Open",
            indication="Diverticulitis",
            notes="initial note",
        ),
    )
    _seed_state(paths, _state_row("UCD-FIL-001"))
    return paths, vocab_dir, paths.manifest_csv.read_bytes()


def _assert_no_mutation(paths: NasPaths, before_manifest: bytes) -> None:
    assert paths.manifest_csv.read_bytes() == before_manifest, (
        "case_manifest.csv changed during a dry-run"
    )
    assert not paths.audit_log.exists(), (
        "pipeline.log was created during a dry-run"
    )


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("case_year", "2027"),
        ("or_room", "OR12"),
        ("procedure_primary", "Sigmoidectomy"),
        ("approach", "Robotic"),
        ("indication", "Colorectal cancer"),
        ("notes", "updated note text"),
    ],
)
def test_dry_run_valid_for_each_editable_field(tmp_path, field, new_value):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        field,
        new_value,
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0, f"stderr was:\n{result.stderr}"
    out = result.stdout
    assert "DRY RUN" in out
    assert "Add --confirm to commit" in out
    assert "Case:" in out
    assert "UCD-FIL-001" in out
    assert f"Field:" in out
    assert field in out
    assert "Before:" in out
    assert "After:" in out
    assert new_value in out
    _assert_no_mutation(paths, before)


def test_dry_run_bad_picklist_value_rejected(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "procedure_primary",
        "Floogle",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    err = result.stderr
    assert "validation error" in err
    assert "procedure_primary" in err
    assert "Floogle" in err
    assert "procedures vocabulary" in err
    _assert_no_mutation(paths, before)


def test_dry_run_bad_year_format_rejected(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "case_year",
        "20XX",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    err = result.stderr
    assert "validation error" in err
    assert "case_year" in err
    assert "4-digit year" in err
    assert "20XX" in err
    _assert_no_mutation(paths, before)


@pytest.mark.parametrize("empty_value", ["", "   ", "\t\t"])
def test_dry_run_empty_or_room_rejected(tmp_path, empty_value):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "or_room",
        empty_value,
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    err = result.stderr
    assert "validation error" in err
    assert "or_room" in err
    assert "non-empty" in err
    _assert_no_mutation(paths, before)


def test_dry_run_notes_accepts_empty_string(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "notes",
        "",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0, f"stderr was:\n{result.stderr}"
    out = result.stdout
    assert "DRY RUN" in out
    # Before is "initial note", After is empty → rendered as "(empty)".
    assert "initial note" in out
    assert "(empty)" in out
    _assert_no_mutation(paths, before)


def test_dry_run_picklist_missing_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    vocab_dir = tmp_path / "vocab"
    # PIPELINE_PICKLIST_DIR exists but procedure_colorectal.json omitted.
    _seed_picklist(vocab_dir / "picklists", "procedure", "colorectal", None)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    before = paths.manifest_csv.read_bytes()

    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "procedure_primary",
        "Sigmoidectomy",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 2
    err = result.stderr
    assert "infrastructure error" in err
    assert "missing" in err
    assert "procedure_colorectal.json" in err
    _assert_no_mutation(paths, before)


def test_dry_run_picklist_malformed_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    vocab_dir = tmp_path / "vocab"
    _seed_picklist(
        vocab_dir / "picklists", "procedure", "colorectal", "{not valid json"
    )
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    before = paths.manifest_csv.read_bytes()

    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "procedure_primary",
        "Sigmoidectomy",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 2
    err = result.stderr
    assert "infrastructure error" in err
    assert "malformed" in err
    assert "procedure_colorectal.json" in err
    _assert_no_mutation(paths, before)


def test_dry_run_case_not_in_manifest_exits_1(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-999",
        "--edit",
        "case_year",
        "2027",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    assert "case not found in manifest: UCD-FIL-999" in result.stderr
    _assert_no_mutation(paths, before)


def test_dry_run_bad_case_format_exits_1(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-99",
        "--edit",
        "case_year",
        "2027",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    assert "invalid case ID format: UCD-FIL-99" in result.stderr
    _assert_no_mutation(paths, before)


def test_dry_run_no_op_edit_preview_shows_before_equal_after(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    # Manifest seeded with case_year=2026; edit to the same value.
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "case_year",
        "2026",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0, f"stderr was:\n{result.stderr}"
    out = result.stdout
    assert "DRY RUN" in out
    # Both Before and After should show 2026.
    lines = out.splitlines()
    before_lines = [ln for ln in lines if ln.startswith("Before:")]
    after_lines = [ln for ln in lines if ln.startswith("After:")]
    assert len(before_lines) == 1 and len(after_lines) == 1
    assert "2026" in before_lines[0]
    assert "2026" in after_lines[0]
    _assert_no_mutation(paths, before)


def test_dry_run_no_audit_log_written_across_failure_modes(tmp_path):
    """Aggregate negative-path assertion: across every failure mode,
    pipeline.log must remain non-existent."""
    paths, vocab_dir, before = _setup_dry_run(tmp_path)

    invocations = [
        # case not in manifest
        ("UCD-FIL-999", "case_year", "2027"),
        # bad case format
        ("FOOBAR", "case_year", "2027"),
        # bad year
        ("UCD-FIL-001", "case_year", "20XX"),
        # bad picklist
        ("UCD-FIL-001", "procedure_primary", "Floogle"),
        # empty or_room
        ("UCD-FIL-001", "or_room", "   "),
    ]
    for case_id, field, value in invocations:
        result = run(
            "metadata",
            case_id,
            "--edit",
            field,
            value,
            env=_env(paths, vocab_dir),
        )
        assert result.returncode in (1, 2)

    _assert_no_mutation(paths, before)


# ----- --edit --confirm commit tests -----


@pytest.mark.parametrize(
    "field,new_value,initial",
    [
        ("case_year", "2027", "2026"),
        ("or_room", "OR12", "OR4"),
        ("procedure_primary", "Sigmoidectomy", "Other"),
        ("approach", "Robotic", "Open"),
        ("indication", "Colorectal cancer", "Diverticulitis"),
        ("notes", "post-edit note", "initial note"),
    ],
)
def test_commit_success_each_field(tmp_path, field, new_value, initial):
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        field,
        new_value,
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = result.stdout
    assert "Committed." in out
    assert "UCD-FIL-001" in out
    assert field in out
    assert new_value in out

    manifest = _read_manifest(paths)
    assert getattr(manifest["UCD-FIL-001"], field) == new_value

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["command"] == "metadata"
    assert e["outcome"] == "success"
    assert e["case"] == "UCD-FIL-001"
    assert e["args"] == {
        "edit_field": field,
        "edit_value": new_value,
        "confirm": True,
    }
    assert e["details"]["field"] == field
    assert e["details"]["before"] == initial
    assert e["details"]["after"] == new_value


def test_commit_idempotent_before_equals_after(tmp_path):
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "case_year",
        "2026",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0
    out = result.stdout
    assert "Committed." in out

    manifest = _read_manifest(paths)
    assert manifest["UCD-FIL-001"].case_year == "2026"

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "success"
    assert entries[0]["details"]["before"] == "2026"
    assert entries[0]["details"]["after"] == "2026"


def test_commit_bad_picklist_logs_validation_failure(tmp_path):
    paths, vocab_dir, before_csv = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "procedure_primary",
        "Floogle",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    assert "validation error" in result.stderr
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["failure_kind"] == "validation"
    assert e["details"]["field"] == "procedure_primary"
    assert e["details"]["value"] == "Floogle"


def test_commit_case_not_in_manifest_logs_not_found(tmp_path):
    paths, vocab_dir, before_csv = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-999",
        "--edit",
        "case_year",
        "2027",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    assert "case not found in manifest: UCD-FIL-999" in result.stderr
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    assert e["case"] == "UCD-FIL-999"
    assert e["details"]["failure_kind"] == "not_found"


def test_commit_bad_case_format_logs_format_failure(tmp_path):
    paths, vocab_dir, before_csv = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "FOOBAR",
        "--edit",
        "case_year",
        "2027",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    assert "invalid case ID format: FOOBAR" in result.stderr
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    # No case key — the arg isn't a real case ID
    assert "case" not in e
    assert e["details"]["failure_kind"] == "format"
    assert e["details"]["case_arg"] == "FOOBAR"


def test_commit_picklist_missing_logs_infra_failure(tmp_path):
    paths = _make_paths(tmp_path)
    vocab_dir = tmp_path / "vocab"
    _seed_picklist(vocab_dir / "picklists", "procedure", "colorectal", None)
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    before_csv = paths.manifest_csv.read_bytes()

    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "procedure_primary",
        "Sigmoidectomy",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 2
    assert "infrastructure error" in result.stderr
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["failure_kind"] == "infra"
    assert "missing" in e["details"]["detail"]


def test_commit_exception_inside_transaction_logs_exception_failure(
    tmp_path, monkeypatch
):
    paths, vocab_dir, before_csv = _setup_dry_run(tmp_path)
    monkeypatch.setenv(
        "PIPELINE_PICKLIST_DIR", str(vocab_dir / "picklists")
    )
    monkeypatch.setenv("PIPELINE_NAS_ROOT", str(paths.root))

    from pipeline import csv_io
    from pipeline.commands import metadata as meta_mod

    def boom(self, tx):
        raise RuntimeError("simulated tempfile rename failure")

    monkeypatch.setattr(csv_io.CsvTable, "_commit", boom)

    args = Namespace(
        ucd_fil_id="UCD-FIL-001",
        edit=["case_year", "2027"],
        confirm=True,
        show=False,
    )
    rc = meta_mod.run(args, paths=paths)
    assert rc == 2
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["failure_kind"] == "exception"
    # Traceback contents are environment-specific, but the marker text must
    # appear so we know the right exception was captured.
    assert "RuntimeError" in e["details"]["error"]
    assert "simulated tempfile rename failure" in e["details"]["error"]


def test_commit_notes_field_writes_nudge_to_stderr(tmp_path):
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "notes",
        "new note",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0
    assert "Committed." in result.stdout
    assert "free-text field" in result.stderr
    assert "surgeon-audit" in result.stderr


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("case_year", "2027"),
        ("or_room", "OR12"),
        ("procedure_primary", "Sigmoidectomy"),
        ("approach", "Robotic"),
        ("indication", "Colorectal cancer"),
    ],
)
def test_commit_non_notes_field_emits_no_nudge(tmp_path, field, new_value):
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        field,
        new_value,
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 0
    assert "free-text field" not in result.stderr
    assert "surgeon-audit" not in result.stderr


def test_commit_audit_entry_has_required_top_level_fields(tmp_path):
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)
    run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "case_year",
        "2027",
        "--confirm",
        env=_env(paths, vocab_dir),
    )
    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    for key in (
        "ts",
        "pid",
        "operator",
        "command",
        "args",
        "outcome",
        "case",
        "details",
    ):
        assert key in e, f"missing required audit key {key!r}: {e!r}"


def test_commit_audit_before_uses_locked_snapshot_not_pre_snapshot(
    tmp_path, monkeypatch
):
    """Inject a stale pre-snapshot via CsvTable.snapshot() monkeypatch while
    leaving CsvTable.transaction() unpatched. The audit's `before` must
    reflect the locked read, not the stale pre-snapshot."""
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)
    monkeypatch.setenv(
        "PIPELINE_PICKLIST_DIR", str(vocab_dir / "picklists")
    )
    monkeypatch.setenv("PIPELINE_NAS_ROOT", str(paths.root))

    from pipeline import csv_io
    from pipeline.commands import metadata as meta_mod

    real_snapshot = csv_io.CsvTable.snapshot

    def stale(self):
        rows = real_snapshot(self)
        # Substitute case_year=1999 for UCD-FIL-001 in the pre-snapshot view.
        return [
            r.model_copy(update={"case_year": "1999"})
            if hasattr(r, "case_year") and r.ucd_fil_id == "UCD-FIL-001"
            else r
            for r in rows
        ]

    monkeypatch.setattr(csv_io.CsvTable, "snapshot", stale)

    args = Namespace(
        ucd_fil_id="UCD-FIL-001",
        edit=["case_year", "2027"],
        confirm=True,
        show=False,
    )
    rc = meta_mod.run(args, paths=paths)
    assert rc == 0

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["details"]["before"] == "2026", (
        f"audit before should reflect the locked snapshot (the real on-disk "
        f"value 2026), not the stale pre-snapshot value 1999. Got: "
        f"{e['details']['before']!r}"
    )
    assert e["details"]["after"] == "2027"


def test_commit_exactly_one_audit_entry_per_invocation(tmp_path):
    """Across two consecutive commit attempts (one success, one failure),
    the audit log should contain exactly two entries."""
    paths, vocab_dir, _ = _setup_dry_run(tmp_path)

    # Attempt 1: success
    rc1 = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "or_room",
        "OR8",
        "--confirm",
        env=_env(paths, vocab_dir),
    ).returncode
    assert rc1 == 0

    # Attempt 2: validation failure
    rc2 = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "procedure_primary",
        "Floogle",
        "--confirm",
        env=_env(paths, vocab_dir),
    ).returncode
    assert rc2 == 1

    entries = _read_audit_entries(paths)
    assert len(entries) == 2
    assert entries[0]["outcome"] == "success"
    assert entries[1]["outcome"] == "failure"
    assert entries[1]["details"]["failure_kind"] == "validation"


# ----- case_years allowlist tests -----


@pytest.mark.parametrize(
    "year,valid",
    [
        ("2014", False),  # just below lower bound
        ("2015", True),   # lower bound, valid
        ("2030", True),   # upper bound, valid
        ("2031", False),  # just above upper bound
    ],
)
def test_dry_run_case_year_boundary(tmp_path, year, valid):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    # Seed the full real allowlist range (2015..2030) so boundaries match
    # prod. After Spec D case_year reads from the picklist seed pattern.
    _seed_picklist(
        vocab_dir / "picklists",
        "case_year",
        None,
        [str(y) for y in range(2015, 2031)],
    )
    result = run(
        "metadata",
        "UCD-FIL-001",
        "--edit",
        "case_year",
        year,
        env=_env(paths, vocab_dir),
    )
    if valid:
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "DRY RUN" in result.stdout
        assert year in result.stdout
    else:
        assert result.returncode == 1
        err = result.stderr
        assert "validation error" in err
        assert "case_year" in err
        assert "not in case_years allowlist" in err
        assert year in err
    _assert_no_mutation(paths, before)


def test_dry_run_format_failure_vs_allowlist_failure_distinct_messages(tmp_path):
    paths, vocab_dir, before = _setup_dry_run(tmp_path)
    _seed_picklist(
        vocab_dir / "picklists",
        "case_year",
        None,
        [str(y) for y in range(2015, 2031)],
    )

    # "20XX" fails the regex first — message mentions "4-digit year".
    bad_format = run(
        "metadata", "UCD-FIL-001", "--edit", "case_year", "20XX",
        env=_env(paths, vocab_dir),
    )
    assert bad_format.returncode == 1
    assert "expected 4-digit year" in bad_format.stderr
    assert "allowlist" not in bad_format.stderr

    # "1999" passes the regex but fails the allowlist — distinct message.
    bad_allowlist = run(
        "metadata", "UCD-FIL-001", "--edit", "case_year", "1999",
        env=_env(paths, vocab_dir),
    )
    assert bad_allowlist.returncode == 1
    assert "not in case_years allowlist" in bad_allowlist.stderr
    assert "4-digit year" not in bad_allowlist.stderr

    _assert_no_mutation(paths, before)


def test_commit_invalid_year_logs_validation_failure_kind(tmp_path):
    paths, vocab_dir, before_csv = _setup_dry_run(tmp_path)
    _seed_picklist(
        vocab_dir / "picklists",
        "case_year",
        None,
        [str(y) for y in range(2015, 2031)],
    )

    result = run(
        "metadata", "UCD-FIL-001", "--edit", "case_year", "1999", "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 1
    assert "validation error" in result.stderr
    assert "1999" in result.stderr
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["failure_kind"] == "validation"
    assert e["details"]["field"] == "case_year"
    assert e["details"]["value"] == "1999"
    assert "allowlist" in e["details"]["reason"]


def test_commit_case_year_picklist_missing_logs_infra_failure(tmp_path):
    paths = _make_paths(tmp_path)
    # Build a vocab_dir with picklists for the other three fields seeded but
    # case_year.json omitted, so validation hits the picklist-missing path.
    vocab_dir = tmp_path / "vocab"
    pdir = vocab_dir / "picklists"
    _seed_picklist(pdir, "procedure", "colorectal", _DEFAULT_PROCEDURES)
    _seed_picklist(pdir, "approach", None, _DEFAULT_APPROACHES)
    _seed_picklist(pdir, "indication", "colorectal", _DEFAULT_INDICATIONS)
    _seed_picklist(pdir, "case_year", None, None)  # missing
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    before_csv = paths.manifest_csv.read_bytes()

    result = run(
        "metadata", "UCD-FIL-001", "--edit", "case_year", "2027", "--confirm",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 2
    err = result.stderr
    assert "infrastructure error" in err
    assert "missing" in err
    assert "case_year.json" in err
    assert paths.manifest_csv.read_bytes() == before_csv

    entries = _read_audit_entries(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "failure"
    assert e["case"] == "UCD-FIL-001"
    assert e["details"]["failure_kind"] == "infra"


def test_dry_run_case_year_picklist_malformed_returns_2(tmp_path):
    paths = _make_paths(tmp_path)
    vocab_dir = tmp_path / "vocab"
    pdir = vocab_dir / "picklists"
    _seed_picklist(pdir, "procedure", "colorectal", _DEFAULT_PROCEDURES)
    _seed_picklist(pdir, "approach", None, _DEFAULT_APPROACHES)
    _seed_picklist(pdir, "indication", "colorectal", _DEFAULT_INDICATIONS)
    _seed_picklist(pdir, "case_year", None, "{not valid json")
    _seed_manifest(paths, _manifest_row("UCD-FIL-001"))
    before_csv = paths.manifest_csv.read_bytes()

    result = run(
        "metadata", "UCD-FIL-001", "--edit", "case_year", "2027",
        env=_env(paths, vocab_dir),
    )
    assert result.returncode == 2
    assert "infrastructure error" in result.stderr
    assert "malformed" in result.stderr
    assert "case_year.json" in result.stderr
    assert paths.manifest_csv.read_bytes() == before_csv
    # Dry-run path — no audit entry.
    assert not paths.audit_log.exists()
