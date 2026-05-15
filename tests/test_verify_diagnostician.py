import json

import httpx
import ollama
import pytest

from pipeline import diagnostician
from pipeline.diagnostician import (
    DiagnosticianInfraError,
    diagnose,
)
from pipeline.schemas import DiagnosticianVerdict


_VALID_PASS = {
    "verdict": "pass",
    "reason": "Zero audio streams, encoder=Lavf60.16.100, h264 codec confirmed.",
    "evidence": [
        "format.tags.encoder=Lavf60.16.100",
        "video codec=h264",
        "audio stream count=0",
    ],
}

_VALID_FAIL = {
    "verdict": "fail",
    "reason": "AAC audio stream still present in the deid output.",
    "evidence": ["streams[1].codec_type=audio", "streams[1].codec_name=aac"],
}


def _make_response(content_obj):
    return {"message": {"content": json.dumps(content_obj)}}


def _make_raw_response(raw_str):
    return {"message": {"content": raw_str}}


@pytest.fixture
def evidence():
    return {
        "ffprobe": {"streams": [{"codec_type": "video"}], "format": {"tags": {}}},
        "exiftool": {},
        "ffmpeg_stderr": "Stream #0:0: Video: h264",
    }


def test_pass_verdict_validated(monkeypatch, evidence):
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return _make_response(_VALID_PASS)

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    result = diagnose(evidence)
    assert isinstance(result, DiagnosticianVerdict)
    assert result.verdict == "pass"
    assert result.reason.startswith("Zero audio streams")
    assert len(result.evidence) == 3
    assert len(calls) == 1
    assert calls[0]["model"] == "qwen3:32b"
    assert calls[0]["format"] == "json"
    assert calls[0]["options"]["temperature"] == 0
    assert calls[0]["think"] is False


def test_fail_verdict_validated(monkeypatch, evidence):
    monkeypatch.setattr(
        diagnostician._ollama_client, "chat", lambda **kw: _make_response(_VALID_FAIL)
    )
    result = diagnose(evidence)
    assert result.verdict == "fail"
    assert "AAC" in result.reason


def test_malformed_json_then_valid_retries_once(monkeypatch, evidence):
    calls = {"n": 0}

    def fake_chat(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _make_raw_response("this is not json {{")
        return _make_response(_VALID_PASS)

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    result = diagnose(evidence)
    assert result.verdict == "pass"
    assert calls["n"] == 2


def test_malformed_json_twice_raises_infra_error(monkeypatch, evidence):
    monkeypatch.setattr(
        diagnostician._ollama_client, "chat", lambda **kw: _make_raw_response("nope {")
    )
    with pytest.raises(DiagnosticianInfraError) as exc_info:
        diagnose(evidence)
    assert exc_info.value.reason == "malformed_output"
    assert len(exc_info.value.raw_outputs) == 2


def test_missing_required_field_then_valid_retries(monkeypatch, evidence):
    calls = {"n": 0}

    def fake_chat(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _make_response({"verdict": "pass"})  # missing reason, evidence
        return _make_response(_VALID_PASS)

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    result = diagnose(evidence)
    assert result.verdict == "pass"
    assert calls["n"] == 2


def test_missing_required_field_twice_raises_infra_error(monkeypatch, evidence):
    monkeypatch.setattr(
        diagnostician._ollama_client,
        "chat",
        lambda **kw: _make_response({"verdict": "pass"}),
    )
    with pytest.raises(DiagnosticianInfraError) as exc_info:
        diagnose(evidence)
    assert exc_info.value.reason == "malformed_output"
    assert len(exc_info.value.raw_outputs) == 2


def test_extra_field_rejected_by_schema(monkeypatch, evidence):
    """DiagnosticianVerdict has extra='forbid' — surplus keys cause
    ValidationError, which triggers retry and ultimately infra error."""
    bad = {**_VALID_PASS, "surplus_field": "nope"}
    monkeypatch.setattr(
        diagnostician._ollama_client, "chat", lambda **kw: _make_response(bad)
    )
    with pytest.raises(DiagnosticianInfraError) as exc_info:
        diagnose(evidence)
    assert exc_info.value.reason == "malformed_output"


def test_connect_error_no_retry(monkeypatch, evidence):
    calls = {"n": 0}

    def fake_chat(**kwargs):
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    with pytest.raises(DiagnosticianInfraError) as exc_info:
        diagnose(evidence)
    assert exc_info.value.reason == "ollama_unavailable"
    assert "connection refused" in exc_info.value.error
    assert calls["n"] == 1


def test_timeout_raises_runtimeerror_not_infra_error(monkeypatch, evidence):
    """F-001: A wall-clock timeout from the ollama client should surface as a
    RuntimeError so verify's generic exception handler records it as a per-case
    stage failure, NOT as DiagnosticianInfraError(reason="ollama_unavailable")
    which would abort the entire batch. A single slow case must not stall every
    other case in the iteration."""
    calls = {"n": 0}

    def fake_chat(**kwargs):
        calls["n"] += 1
        raise httpx.TimeoutException("read timeout")

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    with pytest.raises(RuntimeError) as exc_info:
        diagnose(evidence)
    msg = str(exc_info.value)
    assert "timed out" in msg
    assert str(diagnostician._OLLAMA_CHAT_TIMEOUT_S) in msg
    assert calls["n"] == 1  # no retry on timeout


def test_ollama_response_error_no_retry(monkeypatch, evidence):
    calls = {"n": 0}

    def fake_chat(**kwargs):
        calls["n"] += 1
        raise ollama.ResponseError("model not found", status_code=404)

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    with pytest.raises(DiagnosticianInfraError) as exc_info:
        diagnose(evidence)
    assert exc_info.value.reason == "ollama_unavailable"
    assert calls["n"] == 1


# ----- F-002: collect_evidence subprocess timeouts -----


def _patch_subprocess_run_timeout_for(monkeypatch, target_argv0: str):
    """Make subprocess.run raise TimeoutExpired when invoked with the given
    argv[0] (e.g. "ffprobe", "exiftool", "ffmpeg"). Other invocations pass
    through unchanged so unrelated calls aren't affected."""
    import subprocess as real_subprocess
    original = real_subprocess.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == target_argv0:
            raise real_subprocess.TimeoutExpired(
                cmd=argv, timeout=kwargs.get("timeout", 0)
            )
        return original(argv, **kwargs)

    monkeypatch.setattr(diagnostician.subprocess, "run", fake_run)


def test_collect_evidence_ffprobe_timeout_raises_runtime_error(
    monkeypatch, tmp_path
):
    """F-002: ffprobe TimeoutExpired must be converted to RuntimeError naming
    the tool, the timeout value, and the offending path so verify's generic
    exception handler records a stage failure with operator-readable detail."""
    deid = tmp_path / "UCD-FIL-001_video.mp4"
    deid.write_bytes(b"stub")

    _patch_subprocess_run_timeout_for(monkeypatch, "ffprobe")

    with pytest.raises(RuntimeError) as exc_info:
        diagnostician.collect_evidence(deid)
    msg = str(exc_info.value)
    assert "ffprobe" in msg
    assert "timed out" in msg
    assert str(diagnostician._FFPROBE_TIMEOUT_S) in msg
    assert str(deid) in msg


def test_collect_evidence_exiftool_timeout_raises_runtime_error(
    monkeypatch, tmp_path
):
    """F-002: exiftool TimeoutExpired path. ffprobe is allowed to succeed so
    we exercise the second subprocess slot specifically."""
    deid = tmp_path / "UCD-FIL-002_video.mp4"
    deid.write_bytes(b"stub")

    # ffprobe stub succeeds with a minimal valid response so we reach exiftool.
    import subprocess as real_subprocess

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ffprobe":
            return real_subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout='{"streams": [], "format": {}}', stderr="",
            )
        if argv and argv[0] == "exiftool":
            raise real_subprocess.TimeoutExpired(
                cmd=argv, timeout=kwargs.get("timeout", 0)
            )
        # Don't expect to reach ffmpeg in this test.
        return real_subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="[]", stderr=""
        )

    monkeypatch.setattr(diagnostician.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        diagnostician.collect_evidence(deid)
    msg = str(exc_info.value)
    assert "exiftool" in msg
    assert "timed out" in msg
    assert str(diagnostician._EXIFTOOL_TIMEOUT_S) in msg
    assert str(deid) in msg


def test_collect_evidence_nullmux_timeout_raises_runtime_error(
    monkeypatch, tmp_path
):
    """F-002: ffmpeg null-mux TimeoutExpired path. ffprobe + exiftool stubbed
    to succeed so we reach the third subprocess slot."""
    deid = tmp_path / "UCD-FIL-003_video.mp4"
    deid.write_bytes(b"stub")

    import subprocess as real_subprocess

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "ffprobe":
            return real_subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout='{"streams": [], "format": {}}', stderr="",
            )
        if argv and argv[0] == "exiftool":
            return real_subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="[{}]", stderr=""
            )
        if argv and argv[0] == "ffmpeg":
            raise real_subprocess.TimeoutExpired(
                cmd=argv, timeout=kwargs.get("timeout", 0)
            )
        return real_subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(diagnostician.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        diagnostician.collect_evidence(deid)
    msg = str(exc_info.value)
    assert "ffmpeg" in msg
    assert "null-mux" in msg
    assert "timed out" in msg
    assert str(diagnostician._NULLMUX_TIMEOUT_S) in msg
    assert str(deid) in msg


def test_prompt_contains_evidence_blocks(monkeypatch, evidence):
    """The prompt must embed all three evidence sources."""
    captured = {}

    def fake_chat(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _make_response(_VALID_PASS)

    monkeypatch.setattr(diagnostician._ollama_client, "chat", fake_chat)
    diagnose(evidence)
    prompt = captured["messages"][0]["content"]
    assert "ffprobe" in prompt
    assert "exiftool" in prompt
    assert "Stream #0:0: Video: h264" in prompt
    assert "DiagnosticianVerdict" in prompt or '"verdict"' in prompt
