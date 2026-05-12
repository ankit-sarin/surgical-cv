import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "pipeline", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_top_level_help_lists_all_subcommands():
    result = run("--help")
    assert result.returncode == 0
    for sub in ("concat", "deid", "verify", "status", "metadata"):
        assert sub in result.stdout


def test_concat_help_documents_surgeon_flag():
    result = run("concat", "--help")
    assert result.returncode == 0
    assert "--surgeon" in result.stdout


def test_concat_no_intake_cases(tmp_path):
    nas = tmp_path / "nas"
    (nas / "or-raw").mkdir(parents=True)
    env = {**os.environ, "PIPELINE_NAS_ROOT": str(nas)}
    result = run("concat", "--surgeon", "sarin", env=env)
    assert result.returncode == 0
    assert "No intake cases for surgeon=sarin" in result.stdout


def test_concat_missing_required_arg_errors():
    result = run("concat")
    assert result.returncode != 0
    assert "--surgeon" in result.stderr


def test_deid_help_documents_surgeon_flag():
    result = run("deid", "--help")
    assert result.returncode == 0
    assert "--surgeon" in result.stdout


def test_deid_no_concatenated_cases(tmp_path):
    nas = tmp_path / "nas"
    (nas / "or-raw").mkdir(parents=True)
    env = {**os.environ, "PIPELINE_NAS_ROOT": str(nas)}
    result = run("deid", "--surgeon", "sarin", env=env)
    assert result.returncode == 0
    assert "No concatenated cases for surgeon=sarin" in result.stdout


def test_deid_missing_surgeon_errors():
    result = run("deid")
    assert result.returncode != 0
    assert "--surgeon" in result.stderr


def test_verify_stub_invocation():
    result = run("verify", "/deid/path.mp4")
    assert result.returncode == 0
    assert "Not yet implemented: verify" in result.stdout
    assert "deid_file=/deid/path.mp4" in result.stdout


def test_verify_missing_positional_errors():
    result = run("verify")
    assert result.returncode != 0


def test_status_help_lists_flags():
    result = run("status", "--help")
    assert result.returncode == 0
    for flag in ("--case", "--stage", "--json"):
        assert flag in result.stdout


def test_status_bare_empty_state(tmp_path):
    nas = tmp_path / "nas"
    (nas / "or-raw").mkdir(parents=True)
    env = {**os.environ, "PIPELINE_NAS_ROOT": str(nas)}
    result = run("status", env=env)
    assert result.returncode == 0
    assert "(no cases match)" in result.stdout


def test_status_with_all_flags_no_match(tmp_path):
    import json as _json
    nas = tmp_path / "nas"
    (nas / "or-raw").mkdir(parents=True)
    env = {**os.environ, "PIPELINE_NAS_ROOT": str(nas)}
    result = run("status", "--case", "UCD-FIL-001", "--stage", "verified", "--json", env=env)
    assert result.returncode == 0
    payload = _json.loads(result.stdout)
    assert payload == {"error": "case_not_found", "case": "UCD-FIL-001"}


def test_status_invalid_stage_errors():
    result = run("status", "--stage", "bogus")
    assert result.returncode != 0


def test_metadata_show_default():
    result = run("metadata", "UCD-FIL-001")
    assert result.returncode == 0
    assert "Not yet implemented: metadata" in result.stdout
    assert "ucd_fil_id=UCD-FIL-001" in result.stdout


def test_metadata_edit_with_confirm():
    result = run("metadata", "UCD-FIL-001", "--edit", "case_year", "2026", "--confirm")
    assert result.returncode == 0
    out = result.stdout
    assert "ucd_fil_id=UCD-FIL-001" in out
    assert "edit_field=case_year" in out
    assert "edit_value=2026" in out
    assert "confirm=True" in out


def test_metadata_confirm_without_edit_errors():
    result = run("metadata", "UCD-FIL-001", "--confirm")
    assert result.returncode != 0
    assert "--confirm" in result.stderr or "--edit" in result.stderr


def test_metadata_show_and_edit_are_mutually_exclusive():
    result = run("metadata", "UCD-FIL-001", "--show", "--edit", "f", "v")
    assert result.returncode != 0


def test_metadata_missing_positional_errors():
    result = run("metadata")
    assert result.returncode != 0


def test_no_subcommand_errors():
    result = run()
    assert result.returncode != 0
