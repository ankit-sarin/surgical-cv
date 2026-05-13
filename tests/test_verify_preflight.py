from pipeline.commands.verify import (
    PreflightFailure,
    PreflightResult,
    run_preflight,
)


def _ffprobe(streams=None, format_tags=None):
    return {
        "streams": streams if streams is not None else [{"codec_type": "video", "codec_name": "h264"}],
        "format": {"tags": format_tags or {}},
    }


def test_pf1_audio_stream_present_fails():
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        ),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert isinstance(result.first, PreflightFailure)
    assert result.first.check_id == "PF1"
    assert result.first.detail["audio_stream_count"] == 1


def test_pf1_two_audio_streams_reports_count():
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {"codec_type": "video"},
                {"codec_type": "audio"},
                {"codec_type": "audio"},
            ]
        ),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF1"
    assert result.first.detail["audio_stream_count"] == 2


def test_pf1_zero_audio_streams_passes():
    ev = {
        "ffprobe": _ffprobe(),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is True
    assert result.failures == []


def test_pf2_title_in_format_tags_fails():
    ev = {
        "ffprobe": _ffprobe(format_tags={"title": "Smith, John MRN12345"}),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"
    assert result.first.detail["field"].lower() == "title"
    assert result.first.detail["source"] == "format.tags"
    assert "Smith" in result.first.detail["value"]


def test_pf2_title_in_stream_tags_fails():
    ev = {
        "ffprobe": _ffprobe(
            streams=[{"codec_type": "video", "tags": {"title": "leaked"}}]
        ),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"
    assert "streams[0].tags" in result.first.detail["source"]


def test_pf2_lavf_encoder_passes():
    ev = {
        "ffprobe": _ffprobe(format_tags={"encoder": "Lavf60.16.100"}),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is True


def test_pf2_libx264_encoder_passes():
    ev = {
        "ffprobe": _ffprobe(format_tags={"encoder": "libx264"}),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is True


def test_pf2_sony_encoder_fails():
    ev = {
        "ffprobe": _ffprobe(format_tags={"encoder": "Sony AVCHD Camera"}),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"
    assert "Sony" in result.first.detail["value"]


def test_pf2_videohandler_handler_name_passes():
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {"codec_type": "video", "tags": {"handler_name": "VideoHandler"}}
            ]
        ),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is True


def test_pf2_unknown_handler_name_fails():
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {"codec_type": "video", "tags": {"handler_name": "BDV Archive 6.2"}}
            ]
        ),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"


def test_pf2_creator_in_exiftool_fails():
    ev = {
        "ffprobe": _ffprobe(),
        "exiftool": {"Creator": "Dr. Sarin"},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"
    assert result.first.detail["field"] == "Creator"
    assert "Dr. Sarin" in result.first.detail["value"]


def test_pf2_gps_latitude_in_exiftool_fails():
    ev = {
        "ffprobe": _ffprobe(),
        "exiftool": {"GPSLatitude": "38.5449 N"},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"
    assert result.first.detail["field"] == "GPSLatitude"


def test_pf2_case_insensitive_match():
    ev = {
        "ffprobe": _ffprobe(format_tags={"TITLE": "uppercase variant"}),
        "exiftool": {},
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF2"


def test_pf2_clean_metadata_passes():
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {"codec_type": "video", "tags": {"handler_name": "VideoHandler"}}
            ],
            format_tags={"encoder": "Lavf60.16.100", "major_brand": "isom"},
        ),
        "exiftool": {
            "FileType": "MP4",
            "Duration": "01:23:45",
            "VideoCodec": "h264",
        },
    }
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is True
    assert result.failures == []


def test_pf3_good_filename_passes():
    ev = {"ffprobe": _ffprobe(), "exiftool": {}}
    result = run_preflight(ev, "UCD-FIL-001_video.mp4")
    assert result.passed is True


def test_pf3_bad_filename_fails():
    ev = {"ffprobe": _ffprobe(), "exiftool": {}}
    result = run_preflight(ev, "sarin_20260102_video.mp4")
    assert result.passed is False
    assert result.first.check_id == "PF3"
    assert "sarin_20260102_video.mp4" in result.first.reason


def test_pf3_extra_suffix_fails():
    ev = {"ffprobe": _ffprobe(), "exiftool": {}}
    result = run_preflight(ev, "UCD-FIL-001_video.mp4.tmp")
    assert result.passed is False
    assert result.first.check_id == "PF3"


def test_all_three_passing_returns_clean_result():
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "tags": {"handler_name": "VideoHandler"},
                }
            ],
            format_tags={"encoder": "Lavf60.16.100"},
        ),
        "exiftool": {"FileType": "MP4"},
    }
    result = run_preflight(ev, "UCD-FIL-007_video.mp4")
    assert isinstance(result, PreflightResult)
    assert result.passed is True
    assert result.failures == []
    assert result.first is None


def test_pf1_short_circuits_before_pf2():
    """If PF1 fails, PF2/PF3 are not evaluated — only PF1 is in failures."""
    ev = {
        "ffprobe": _ffprobe(
            streams=[
                {"codec_type": "audio"},
            ],
            format_tags={"title": "would also fail PF2"},
        ),
        "exiftool": {},
    }
    result = run_preflight(ev, "bad_filename.mp4")
    assert result.passed is False
    assert len(result.failures) == 1
    assert result.first.check_id == "PF1"
