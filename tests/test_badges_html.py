"""Tests for ``app/badges_html.py`` — pure HTML helpers for the My Cases
tab. Tests assert structural attributes (class names, ``data-*``,
``aria-label``) — never on hex values, which belong to the brand skill.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from app.badges import BadgeState
from app.badges_html import (
    badge_html,
    format_counter_strip,
    format_footer,
    pipeline_timeline_html,
)
from pipeline.schemas import Stage


# ----- badge_html -----


@pytest.mark.parametrize("state", list(BadgeState))
def test_badge_html_carries_state_class_and_data_attribute(state):
    html = badge_html(state)
    assert f'class="ds-badge ds-badge-{state.value}"' in html
    assert f'data-badge="{state.value}"' in html
    assert 'aria-label="Status:' in html


def test_badge_html_label_text_matches_state():
    """Each badge's visible label is the capitalized state name."""
    expected = {
        BadgeState.QUEUED: "Queued",
        BadgeState.PROCESSING: "Processing",
        BadgeState.COMPLETE: "Complete",
        BadgeState.FLAGGED: "Flagged",
        BadgeState.FAILED: "Failed",
        BadgeState.STUCK: "Stuck",
    }
    for state, label in expected.items():
        html = badge_html(state)
        assert f">{label}<" in html


def test_badge_html_six_states_produce_distinct_html():
    """No two badge states should collide on rendered markup."""
    rendered = {state: badge_html(state) for state in BadgeState}
    assert len(set(rendered.values())) == len(BadgeState)


def test_badge_html_has_no_inline_hex_colors():
    """Hex values belong to the brand skill, not this module."""
    pattern = re.compile(r"#[0-9a-fA-F]{3,6}")
    for state in BadgeState:
        assert not pattern.search(badge_html(state)), (
            f"badge_html({state}) leaked an inline hex color"
        )


# ----- pipeline_timeline_html -----


def _state(stage: Stage, **extras) -> dict:
    base = {
        "ucd_fil_id": "UCD-FIL-001",
        "raw_segments": ["a.mp4"],
        "concat_filename": "",
        "deid_filename": "",
        "stage": stage,
        "intake_ts": "",
        "concat_ts": "",
        "deid_ts": "",
        "verify_ts": "",
        "verification_notes": "",
    }
    base.update(extras)
    return base


def _step_classes(html: str) -> list[str]:
    """Return the ``class`` value of every step in the rendered timeline,
    in document order."""
    return re.findall(r'class="(ds-timeline-step[^"]*)"', html)


def test_timeline_for_verified_no_attention_all_filled():
    state = _state(
        Stage.verified,
        intake_ts="2026-05-12T08:00:00+00:00",
        concat_ts="2026-05-12T08:30:00",
        deid_ts="2026-05-12T09:00:00",
        verify_ts="2026-05-12T09:30:00",
    )
    html = pipeline_timeline_html(state, BadgeState.COMPLETE)
    classes = _step_classes(html)
    assert len(classes) == 4
    assert all("is-filled" in c for c in classes)


def test_timeline_for_failed_at_concat_stops_at_step_2():
    """Failed mid-concat: step 1 (Submit) filled; step 2 (Concat) shows
    the failure marker; steps 3-4 stay empty."""
    state = _state(
        Stage.failed,
        intake_ts="2026-05-12T08:00:00+00:00",
        # No concat_ts, no deid_ts, no verify_ts → failure landed at concat.
    )
    html = pipeline_timeline_html(state, BadgeState.FAILED)
    classes = _step_classes(html)
    assert "is-filled" in classes[0]      # Submit
    assert "is-failed" in classes[1]      # Concat
    assert classes[2] == "ds-timeline-step"  # default empty
    assert classes[3] == "ds-timeline-step"


def test_timeline_for_failed_at_deid():
    """concat_ts populated, deid_ts empty → failure landed at deid."""
    state = _state(
        Stage.failed,
        intake_ts="2026-05-12T08:00:00+00:00",
        concat_ts="2026-05-12T08:30:00",
    )
    html = pipeline_timeline_html(state, BadgeState.FAILED)
    classes = _step_classes(html)
    assert "is-filled" in classes[0]
    assert "is-filled" in classes[1]
    assert "is-failed" in classes[2]
    assert classes[3] == "ds-timeline-step"


def test_timeline_for_processing_marks_current_step():
    """Stage=concatenated + Processing badge → step 1-2 filled, step 3
    is current (in-flight), step 4 empty."""
    state = _state(
        Stage.concatenated,
        intake_ts="2026-05-12T08:00:00+00:00",
        concat_ts="2026-05-12T08:30:00",
    )
    html = pipeline_timeline_html(state, BadgeState.PROCESSING)
    classes = _step_classes(html)
    assert "is-filled" in classes[0]
    assert "is-filled" in classes[1]
    assert "is-current" in classes[2]
    assert classes[3] == "ds-timeline-step"


def test_timeline_for_none_state_all_empty():
    html = pipeline_timeline_html(None, BadgeState.QUEUED)
    classes = _step_classes(html)
    assert len(classes) == 4
    assert all(c == "ds-timeline-step" for c in classes)


def test_timeline_for_stuck_marks_step_1_only():
    """Intake stuck > threshold: step 1 (Submit) renders the stuck
    indicator; subsequent steps stay empty."""
    state = _state(
        Stage.intake,
        intake_ts="2026-05-12T08:00:00+00:00",
    )
    html = pipeline_timeline_html(state, BadgeState.STUCK)
    classes = _step_classes(html)
    assert "is-stuck" in classes[0]
    assert all(c == "ds-timeline-step" for c in classes[1:])


def test_timeline_step_labels_match_pipeline_stages():
    html = pipeline_timeline_html(None, BadgeState.QUEUED)
    for label in ("Submit", "Concat", "Deid", "Verify"):
        assert f">{label}<" in html


# ----- format_counter_strip -----


def test_counter_strip_live_data_distribution():
    """Matches the four-cases-all-complete production state described in
    the brief: ``"4 cases · 4 complete · 0 in progress · 0 need attention"``."""
    counts = {BadgeState.COMPLETE: 4}
    assert format_counter_strip(counts) == (
        "4 cases · 4 complete · 0 in progress · 0 need attention"
    )


def test_counter_strip_buckets_in_progress_correctly():
    """Processing + Queued + Stuck all count as in-progress."""
    counts = {
        BadgeState.PROCESSING: 1,
        BadgeState.QUEUED: 2,
        BadgeState.STUCK: 1,
    }
    assert format_counter_strip(counts) == (
        "4 cases · 0 complete · 4 in progress · 0 need attention"
    )


def test_counter_strip_buckets_need_attention():
    """Flagged + Failed both count as need-attention."""
    counts = {BadgeState.FLAGGED: 1, BadgeState.FAILED: 2}
    assert format_counter_strip(counts) == (
        "3 cases · 0 complete · 0 in progress · 3 need attention"
    )


def test_counter_strip_zero_state():
    assert format_counter_strip({}) == (
        "0 cases · 0 complete · 0 in progress · 0 need attention"
    )


# ----- format_footer -----


def test_footer_renders_clock_in_HHMMSS():
    now = datetime(2026, 5, 16, 9, 43, 21, tzinfo=timezone.utc)
    out = format_footer(now)
    assert "Auto-refreshes every 30 s" in out
    assert "09:43:21" in out


def test_footer_changes_with_clock():
    early = datetime(2026, 5, 16, 9, 43, 21, tzinfo=timezone.utc)
    later = datetime(2026, 5, 16, 9, 43, 22, tzinfo=timezone.utc)
    assert format_footer(early) != format_footer(later)
