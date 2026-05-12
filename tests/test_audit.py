import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.audit import log_audit

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_entries(log_path):
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def test_single_entry_one_line(tmp_path):
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {"surgeon": "sarin"}, "success")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["command"] == "concat"
    assert obj["args"] == {"surgeon": "sarin"}
    assert obj["outcome"] == "success"


def test_multiple_sequential_entries_preserved(tmp_path):
    log_path = tmp_path / "audit.log"
    for i in range(5):
        log_audit(log_path, "concat", {"i": i}, "success")
    entries = _read_entries(log_path)
    assert len(entries) == 5
    assert [e["args"]["i"] for e in entries] == [0, 1, 2, 3, 4]


def test_case_key_omitted_when_none(tmp_path):
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "status", {}, "success", case=None)
    obj = _read_entries(log_path)[0]
    assert "case" not in obj


def test_case_key_present_when_supplied(tmp_path):
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {}, "success", case="UCD-FIL-001")
    obj = _read_entries(log_path)[0]
    assert obj["case"] == "UCD-FIL-001"


def test_details_key_omitted_when_none(tmp_path):
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "status", {}, "success", details=None)
    obj = _read_entries(log_path)[0]
    assert "details" not in obj


def test_operator_resolves_from_user_env(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "drsarin")
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {}, "success")
    obj = _read_entries(log_path)[0]
    assert obj["operator"] == "drsarin"


def test_operator_falls_back_to_unknown_when_user_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {}, "success")
    obj = _read_entries(log_path)[0]
    assert obj["operator"] == "unknown"


def test_explicit_operator_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "someoneelse")
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {}, "success", operator="ankit")
    obj = _read_entries(log_path)[0]
    assert obj["operator"] == "ankit"


def test_ts_is_recent_iso_with_utc_offset(tmp_path):
    log_path = tmp_path / "audit.log"
    before = datetime.now(timezone.utc)
    log_audit(log_path, "concat", {}, "success")
    after = datetime.now(timezone.utc)
    obj = _read_entries(log_path)[0]
    parsed = datetime.fromisoformat(obj["ts"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    delta_before = (parsed - before).total_seconds()
    delta_after = (after - parsed).total_seconds()
    assert -5 <= delta_before <= 5
    assert -5 <= delta_after <= 5


def test_pid_matches_caller_pid(tmp_path):
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {}, "success")
    obj = _read_entries(log_path)[0]
    assert obj["pid"] == os.getpid()


def test_non_serializable_details_raises_typeerror_naming_key(tmp_path):
    log_path = tmp_path / "audit.log"
    with pytest.raises(TypeError) as exc_info:
        log_audit(log_path, "concat", {}, "success", details={"obj": object()})
    msg = str(exc_info.value)
    assert "obj" in msg
    assert "details" in msg


def test_non_serializable_args_raises_typeerror_naming_key(tmp_path):
    log_path = tmp_path / "audit.log"
    with pytest.raises(TypeError) as exc_info:
        log_audit(log_path, "concat", {"weird": object()}, "success")
    msg = str(exc_info.value)
    assert "weird" in msg
    assert "args" in msg


def test_parent_directory_created_automatically(tmp_path):
    log_path = tmp_path / "a" / "b" / "audit.log"
    assert not log_path.parent.exists()
    log_audit(log_path, "concat", {}, "success")
    assert log_path.exists()
    assert log_path.parent.is_dir()


def test_compact_json_no_spaces(tmp_path):
    log_path = tmp_path / "audit.log"
    log_audit(log_path, "concat", {"a": 1, "b": 2}, "success")
    line = log_path.read_text().splitlines()[0]
    assert ": " not in line
    assert ", " not in line


def test_concurrent_writes_100_entries(tmp_path):
    log_path = tmp_path / "audit.log"
    worker = f"""
import sys
from pathlib import Path
from pipeline.audit import log_audit

prefix = sys.argv[1]
for i in range(50):
    log_audit(
        Path({str(log_path)!r}),
        'concat',
        {{'prefix': prefix, 'i': i}},
        'success',
    )
"""
    p1 = subprocess.Popen(
        [sys.executable, "-c", worker, "A"], cwd=str(PROJECT_ROOT)
    )
    p2 = subprocess.Popen(
        [sys.executable, "-c", worker, "B"], cwd=str(PROJECT_ROOT)
    )
    assert p1.wait(timeout=60) == 0
    assert p2.wait(timeout=60) == 0
    lines = log_path.read_text().splitlines()
    assert len(lines) == 100
    parsed = [json.loads(line) for line in lines]
    prefixes = {e["args"]["prefix"] for e in parsed}
    assert prefixes == {"A", "B"}
    counts = {"A": 0, "B": 0}
    for e in parsed:
        counts[e["args"]["prefix"]] += 1
    assert counts == {"A": 50, "B": 50}
