import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import ffmpeg as ffmpeg_mod
from pipeline.ffmpeg import (
    BdvFilenameError,
    CodecMismatchError,
    FFmpegError,
    FFmpegToolError,
    FFprobeError,
    check_uniformity,
    ffmpeg_concat,
    ffmpeg_deid,
    ffprobe_streams,
    parse_bdv_timestamp,
    video_stream_signature,
)


def _uniform_probe(codec="h264", w=1920, h=1080, fr="30/1", pix="yuv420p"):
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": w,
                "height": h,
                "avg_frame_rate": fr,
                "pix_fmt": pix,
            }
        ]
    }


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["ffmpeg"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_ffprobe_streams_happy_path(monkeypatch, tmp_path):
    fake = _uniform_probe()
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: _completed(0, json.dumps(fake), ""),
    )
    result = ffprobe_streams(tmp_path / "x.mp4")
    assert result == fake


def test_ffprobe_streams_nonzero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: _completed(1, "", "could not open file"),
    )
    with pytest.raises(FFprobeError) as exc_info:
        ffprobe_streams(tmp_path / "missing.mp4")
    assert "could not open file" in exc_info.value.stderr
    assert exc_info.value.path == tmp_path / "missing.mp4"


def test_ffprobe_streams_invalid_json_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: _completed(0, "not json", ""),
    )
    with pytest.raises(FFprobeError):
        ffprobe_streams(tmp_path / "x.mp4")


def test_video_stream_signature_picks_video_when_audio_first():
    probe = {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "pix_fmt": "yuv420p",
            },
        ]
    }
    sig = video_stream_signature(probe)
    assert sig == ("h264", 1920, 1080, "30/1", "yuv420p")


def test_video_stream_signature_no_video_raises():
    probe = {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}
    with pytest.raises(FFprobeError) as exc_info:
        video_stream_signature(probe)
    assert "no video stream" in str(exc_info.value)


def test_check_uniformity_single_path(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _completed(0, json.dumps(_uniform_probe()), "")

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    check_uniformity([tmp_path / "a.mp4"])
    assert len(calls) == 1


def test_check_uniformity_passes_for_uniform_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: _completed(0, json.dumps(_uniform_probe()), ""),
    )
    check_uniformity([tmp_path / f"{i}.mp4" for i in range(3)])


def test_check_uniformity_raises_on_mismatch(monkeypatch, tmp_path):
    probes = [
        _uniform_probe(),
        _uniform_probe(),
        _uniform_probe(h=720),
    ]
    queue = iter(probes)
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: _completed(0, json.dumps(next(queue)), ""),
    )
    paths = [tmp_path / f"{i}.mp4" for i in range(3)]
    with pytest.raises(CodecMismatchError) as exc_info:
        check_uniformity(paths)
    err = exc_info.value
    assert err.reference_path == paths[0]
    assert err.mismatched_path == paths[2]
    assert err.ref_signature == ("h264", 1920, 1080, "30/1", "yuv420p")
    assert err.mismatched_signature == ("h264", 1920, 720, "30/1", "yuv420p")


def test_check_uniformity_empty_list_raises_value_error():
    with pytest.raises(ValueError, match="at least one"):
        check_uniformity([])


def test_parse_bdv_timestamp_canonical():
    assert parse_bdv_timestamp("capt0_20260101-080000.mp4") == "20260101-080000"


def test_parse_bdv_timestamp_copied_variant():
    assert (
        parse_bdv_timestamp("capt0_20260101-080000-copied.mp4") == "20260101-080000"
    )


def test_parse_bdv_timestamp_rejects_garbage():
    for bad in [
        "garbage.mp4",
        "capt0_2026-080000.mp4",
        "capt0_20260101080000.mp4",
        "capt1_20260101-080000.mp4",
        "capt0_20260101-080000.mov",
        "capt0_20260101-080000-edited.mp4",
        "",
    ]:
        with pytest.raises(BdvFilenameError) as exc_info:
            parse_bdv_timestamp(bad)
        assert exc_info.value.filename == bad


def test_ffmpeg_concat_invokes_subprocess_correctly(monkeypatch, tmp_path):
    seg1 = tmp_path / "a.mp4"
    seg2 = tmp_path / "b.mp4"
    seg1.write_bytes(b"")
    seg2.write_bytes(b"")
    output = tmp_path / "out.mp4"
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = list(argv)
        captured["kwargs"] = kw
        i_idx = argv.index("-i")
        list_path = Path(argv[i_idx + 1])
        captured["list_path"] = list_path
        captured["list_content"] = list_path.read_text()
        return _completed(0, "", "")

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    ffmpeg_concat([seg1, seg2], output)

    argv = captured["argv"]
    assert argv[0] == "ffmpeg"
    assert "-hide_banner" in argv
    assert "-loglevel" in argv and argv[argv.index("-loglevel") + 1] == "error"
    assert "-f" in argv and argv[argv.index("-f") + 1] == "concat"
    assert "-safe" in argv and argv[argv.index("-safe") + 1] == "0"
    assert "-c" in argv and argv[argv.index("-c") + 1] == "copy"
    assert argv[-1] == str(output)

    list_path = captured["list_path"]
    assert list_path.parent == output.parent
    assert not list_path.exists(), "temp list file should be cleaned up on success"

    content = captured["list_content"]
    assert "file '" in content
    assert str(seg1.resolve()) in content
    assert str(seg2.resolve()) in content
    assert content.count("\n") == 2


def test_ffmpeg_concat_raises_on_nonzero_exit(monkeypatch, tmp_path):
    seg = tmp_path / "a.mp4"
    seg.write_bytes(b"")
    output = tmp_path / "out.mp4"

    captured = {}

    def fake_run(argv, **kw):
        i_idx = argv.index("-i")
        captured["list_path"] = Path(argv[i_idx + 1])
        return _completed(2, "", "Invalid data found")

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    with pytest.raises(FFmpegError) as exc_info:
        ffmpeg_concat([seg], output)
    assert exc_info.value.exit_code == 2
    assert "Invalid data found" in exc_info.value.stderr
    assert not captured["list_path"].exists()


def test_ffmpeg_concat_raises_file_exists_if_output_present(monkeypatch, tmp_path):
    seg = tmp_path / "a.mp4"
    seg.write_bytes(b"")
    output = tmp_path / "out.mp4"
    output.write_bytes(b"existing")

    called = []

    def fake_run(argv, **kw):
        called.append(argv)
        return _completed(0, "", "")

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    with pytest.raises(FileExistsError):
        ffmpeg_concat([seg], output)
    assert called == [], "subprocess should not have been invoked"


def test_ffmpeg_concat_rejects_missing_segment(monkeypatch, tmp_path):
    seg = tmp_path / "nope.mp4"
    output = tmp_path / "out.mp4"
    monkeypatch.setattr(
        ffmpeg_mod.subprocess, "run", lambda *a, **kw: _completed(0, "", "")
    )
    with pytest.raises(FileNotFoundError):
        ffmpeg_concat([seg], output)


def test_ffmpeg_deid_invokes_subprocess_with_expected_flags(monkeypatch, tmp_path):
    seg = tmp_path / "input.mp4"
    seg.write_bytes(b"x")
    output = tmp_path / "deid.mp4"
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = list(argv)
        return _completed(0, "", "")

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    ffmpeg_deid(seg, output)

    argv = captured["argv"]
    assert argv[0] == "ffmpeg"
    assert "-hide_banner" in argv
    assert "-loglevel" in argv and argv[argv.index("-loglevel") + 1] == "error"
    assert "-i" in argv and argv[argv.index("-i") + 1] == str(seg)
    assert "-an" in argv
    assert "-map_metadata" in argv and argv[argv.index("-map_metadata") + 1] == "-1"
    assert "-c:v" in argv and argv[argv.index("-c:v") + 1] == "libx264"
    assert "-crf" in argv and argv[argv.index("-crf") + 1] == "18"
    assert "-movflags" in argv and argv[argv.index("-movflags") + 1] == "+faststart"
    assert argv[-1] == str(output)
    assert argv.index("-an") > argv.index("-i")


def test_ffmpeg_deid_nonzero_exit_raises(monkeypatch, tmp_path):
    seg = tmp_path / "input.mp4"
    seg.write_bytes(b"x")
    output = tmp_path / "deid.mp4"
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: _completed(1, "", "encode failure"),
    )
    with pytest.raises(FFmpegError) as exc_info:
        ffmpeg_deid(seg, output)
    assert exc_info.value.exit_code == 1
    assert "encode failure" in exc_info.value.stderr


def test_ffmpeg_deid_output_exists_raises(monkeypatch, tmp_path):
    seg = tmp_path / "input.mp4"
    seg.write_bytes(b"x")
    output = tmp_path / "deid.mp4"
    output.write_bytes(b"existing")
    called = []
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda argv, **kw: called.append(argv) or _completed(0, "", ""),
    )
    with pytest.raises(FileExistsError):
        ffmpeg_deid(seg, output)
    assert called == [], "subprocess should not have been invoked"


def test_ffmpeg_deid_missing_input_raises(monkeypatch, tmp_path):
    seg = tmp_path / "nope.mp4"
    output = tmp_path / "deid.mp4"
    monkeypatch.setattr(
        ffmpeg_mod.subprocess, "run", lambda *a, **kw: _completed(0, "", "")
    )
    with pytest.raises(FileNotFoundError):
        ffmpeg_deid(seg, output)


def test_exception_hierarchy():
    assert issubclass(FFprobeError, FFmpegToolError)
    assert issubclass(CodecMismatchError, FFmpegToolError)
    assert issubclass(FFmpegError, FFmpegToolError)
    assert issubclass(BdvFilenameError, FFmpegToolError)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_integration_real_ffmpeg_concat(tmp_path):
    seg1 = tmp_path / "s1.mp4"
    seg2 = tmp_path / "s2.mp4"
    for seg in (seg1, seg2):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=128x128:rate=30",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(seg),
            ],
            check=True,
            capture_output=True,
        )
    output = tmp_path / "merged.mp4"
    check_uniformity([seg1, seg2])
    ffmpeg_concat([seg1, seg2], output)
    assert output.exists()
    assert output.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_integration_real_ffmpeg_deid(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=128x128:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-metadata",
            "title=PHI title leaks here",
            "-metadata",
            "comment=patient name",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    deid_output = tmp_path / "deid.mp4"
    ffmpeg_deid(source, deid_output)
    assert deid_output.exists()
    probe_result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-of",
            "json",
            "-show_format",
            "-show_streams",
            str(deid_output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = json.loads(probe_result.stdout)
    streams = probe["streams"]
    assert len(streams) == 1, f"expected 1 stream (audio stripped), got {len(streams)}"
    assert streams[0]["codec_type"] == "video"
    tags = probe["format"].get("tags", {})
    assert "title" not in tags
    assert "comment" not in tags
