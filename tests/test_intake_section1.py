"""Tests for the Intake tab's Section 1 (segment selection).

Two layers: pure-Python format helpers and the per-request scope-builder
(unit), and Gradio's Blocks construction shape (smoke). The dynamically
rendered accordion + checkboxes can only be exercised inside a running
Gradio runtime, so that's covered by the uvicorn smoke run rather than
the test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import pytest

from app.auth import SESSION_COOKIE_NAME, encode_session
from app.repos import SegmentRecord
from app.surgeon_app import (
    _EMPTY_STATE_MSG,
    _scope_from_request,
    build_surgeon_app,
    fetch_segments,
    fmt_group_header,
    fmt_segment_label,
)
from pipeline.grouping import group_segments


def _rec(filename: str, year, month, day, hour, minute, size=2_000_000_000):
    return SegmentRecord(
        filename=filename,
        timestamp=datetime(year, month, day, hour, minute, tzinfo=timezone.utc),
        size_bytes=size,
        path=Path(f"/tmp/raw-sarin/{filename}"),
    )


def _make_request(token: str | None):
    cookies = {SESSION_COOKIE_NAME: token} if token else {}
    return SimpleNamespace(cookies=cookies, headers={})


# ----- format helpers -----


def test_fmt_segment_label_today():
    now = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc)
    seg = _rec("capt0_20260102-082045.mp4", 2026, 1, 2, 8, 20, size=1_900_000_000)
    label = fmt_segment_label(seg, now=now)
    assert "08:20" in label
    assert "capt0_20260102-082045.mp4" in label
    assert "1.8 GB" in label  # 1_900_000_000 bytes


def test_fmt_segment_label_same_year_different_day():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    seg = _rec("capt0_20260102-082000.mp4", 2026, 1, 2, 8, 20)
    label = fmt_segment_label(seg, now=now)
    assert "Jan" in label
    assert "08:20" in label


def test_fmt_segment_label_prior_year():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    seg = _rec("capt0_20250102-082000.mp4", 2025, 1, 2, 8, 20)
    label = fmt_segment_label(seg, now=now)
    assert "2025-01-02" in label


def test_fmt_group_header_singular_vs_plural():
    now = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc)
    one_seg = [_rec("capt0_20260102-080000.mp4", 2026, 1, 2, 8, 0)]
    two_segs = [
        _rec("capt0_20260102-080000.mp4", 2026, 1, 2, 8, 0),
        _rec("capt0_20260102-082000.mp4", 2026, 1, 2, 8, 20),
    ]
    one_group = group_segments(one_seg)[0]
    two_group = group_segments(two_segs)[0]
    assert "1 segment" in fmt_group_header(one_group, now=now)
    assert "2 segments" in fmt_group_header(two_group, now=now)


def test_fmt_group_header_totals_size():
    now = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc)
    segs = [
        _rec("capt0_20260102-080000.mp4", 2026, 1, 2, 8, 0, size=1_000_000_000),
        _rec("capt0_20260102-082000.mp4", 2026, 1, 2, 8, 20, size=2_000_000_000),
    ]
    group = group_segments(segs)[0]
    header = fmt_group_header(group, now=now)
    # 3_000_000_000 bytes ≈ 2.8 GB
    assert "GB" in header


# ----- per-request scope construction -----


def test_scope_from_request_with_no_request_returns_none():
    assert _scope_from_request(None) is None


def test_scope_from_request_without_cookie_returns_none(app_env):
    req = _make_request(token=None)
    assert _scope_from_request(req) is None


def test_scope_from_request_with_bad_cookie_returns_none(app_env):
    req = _make_request(token="garbage.not.a.token")
    assert _scope_from_request(req) is None


def test_scope_from_request_with_admin_returns_none(app_env):
    """Admin must not get a SurgeonScope — Intake is surgeon-only."""
    req = _make_request(token=encode_session("ankitsarin"))
    assert _scope_from_request(req) is None


def test_scope_from_request_with_surgeon_returns_scope(app_env):
    req = _make_request(token=encode_session("asarin"))
    scope = _scope_from_request(req)
    assert scope is not None
    assert scope.username == "asarin"
    assert scope.folder_slug == "sarin"


# ----- fetch_segments wired through the real filesystem repo -----


def test_fetch_segments_returns_empty_when_no_raw_folder(app_env):
    """app_env's RAW_VIDEO_ROOT points at an empty tmpdir → no raw-sarin/
    → list_raw_segments returns [], fetch_segments returns []."""
    req = _make_request(token=encode_session("asarin"))
    assert fetch_segments(req) == []


def test_fetch_segments_returns_records_from_real_fs(app_env, tmp_path):
    """Drop a BDV file into raw-sarin/ under the test root; fetch finds it."""
    raw_root_dir = Path(__import__("os").environ["RAW_VIDEO_ROOT"])
    folder = raw_root_dir / "raw-sarin"
    folder.mkdir()
    (folder / "capt0_20260102-082000.mp4").write_bytes(b"\x00" * 1000)
    req = _make_request(token=encode_session("asarin"))
    result = fetch_segments(req)
    assert len(result) == 1
    assert result[0].filename == "capt0_20260102-082000.mp4"


def test_fetch_segments_unauthenticated_returns_empty(app_env):
    """Defense-in-depth: even though /app/ is gated, fetch_segments returns
    [] rather than crashing if reached with no session."""
    assert fetch_segments(_make_request(token=None)) == []


# ----- Blocks construction (static introspection) -----


def test_surgeon_blocks_carries_three_tabs_unchanged():
    blocks = build_surgeon_app()
    labels = [c.label for c in blocks.blocks.values() if isinstance(c, gr.Tab)]
    assert labels == ["Intake", "My Cases", "Action Required"]


def test_intake_tab_includes_refresh_button():
    blocks = build_surgeon_app()
    button_values = [
        c.value for c in blocks.blocks.values() if isinstance(c, gr.Button)
    ]
    assert "Refresh" in button_values


def test_intake_section_carries_state_components():
    """gr.State for segments + selected + show_more is wired so downstream
    sections (Sections 2-5, future) can consume the selection."""
    blocks = build_surgeon_app()
    state_count = sum(
        1 for c in blocks.blocks.values() if isinstance(c, gr.State)
    )
    # 3 gr.State objects in the Intake tab (segments / selected / show_more).
    assert state_count >= 3


# ----- single-source-of-truth: the Intake section uses the same grouper
# the future pipeline-side intake writer will use -----


def test_intake_uses_pipeline_grouping_module():
    """Importing the grouping symbol via the surgeon app module proves both
    callers (UI + any future pipeline-side intake writer) resolve to the
    one canonical function in pipeline.grouping."""
    from app import surgeon_app
    from pipeline.grouping import group_segments as pipeline_grouper

    assert surgeon_app.group_segments is pipeline_grouper


# ----- empty-state copy -----


def test_empty_state_message_mentions_citrix_and_refresh():
    assert "Citrix" in _EMPTY_STATE_MSG
    assert "Refresh" in _EMPTY_STATE_MSG
