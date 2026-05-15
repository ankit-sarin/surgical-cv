"""Tests for ``app/badges.py`` — the pure decision-table function that
maps (state, has_attention, now) → BadgeState. No I/O, all clocks
explicitly anchored."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.badges import BadgeState, derive_badge_state
from pipeline.schemas import Stage


# Single anchor used for all time-sensitive tests so the fixtures stay
# legible at a glance.
NOW = datetime(2026, 5, 16, 14, 0, 0, tzinfo=timezone.utc)


def _state(stage: Stage, **extras) -> dict:
    """Minimal state dict in the shape PipelineStateRepository surfaces."""
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


# ----- the six target states -----


def test_state_none_is_queued():
    """Case manifest row exists but pipeline_state row not yet written
    (worker hasn't dispatched the marker yet). Treat as queued — there is
    no submission timestamp to escalate against."""
    assert derive_badge_state(None, False, NOW) == BadgeState.QUEUED


def test_failed_stage_is_failed():
    s = _state(Stage.failed, verification_notes="diagnostician fail")
    assert derive_badge_state(s, False, NOW) == BadgeState.FAILED


def test_verified_with_attention_is_flagged():
    s = _state(Stage.verified, verify_ts="2026-05-15T10:00:00")
    assert derive_badge_state(s, True, NOW) == BadgeState.FLAGGED


def test_verified_without_attention_is_complete():
    s = _state(Stage.verified, verify_ts="2026-05-15T10:00:00")
    assert derive_badge_state(s, False, NOW) == BadgeState.COMPLETE


def test_concatenated_is_processing():
    s = _state(Stage.concatenated, concat_ts="2026-05-16T13:55:00")
    assert derive_badge_state(s, False, NOW) == BadgeState.PROCESSING


def test_deidentified_is_processing():
    s = _state(Stage.deidentified, deid_ts="2026-05-16T13:55:00")
    assert derive_badge_state(s, False, NOW) == BadgeState.PROCESSING


def test_intake_old_enough_is_stuck():
    """30 minutes old, threshold 15 min → STUCK."""
    s = _state(
        Stage.intake,
        intake_ts=(NOW - timedelta(minutes=30)).isoformat(),
    )
    assert derive_badge_state(s, False, NOW) == BadgeState.STUCK


def test_intake_recent_is_queued():
    """5 minutes old, threshold 15 min → QUEUED (still in the normal
    waiting window)."""
    s = _state(
        Stage.intake,
        intake_ts=(NOW - timedelta(minutes=5)).isoformat(),
    )
    assert derive_badge_state(s, False, NOW) == BadgeState.QUEUED


# ----- threshold boundary -----


def test_intake_exactly_at_threshold_is_stuck():
    """Closed lower bound: age >= threshold trips STUCK. Tested at
    exactly 15 minutes (the default threshold)."""
    s = _state(
        Stage.intake,
        intake_ts=(NOW - timedelta(minutes=15)).isoformat(),
    )
    assert derive_badge_state(s, False, NOW) == BadgeState.STUCK


def test_intake_one_second_under_threshold_is_queued():
    s = _state(
        Stage.intake,
        intake_ts=(NOW - timedelta(minutes=15, seconds=-1)).isoformat(),
    )
    # 14 min 59 sec → still queued
    assert derive_badge_state(s, False, NOW) == BadgeState.QUEUED


def test_custom_threshold():
    """Caller can pass a different threshold; default is 15."""
    s = _state(
        Stage.intake,
        intake_ts=(NOW - timedelta(minutes=10)).isoformat(),
    )
    # 10 min, threshold 5 → STUCK
    assert derive_badge_state(
        s, False, NOW, stuck_threshold_minutes=5
    ) == BadgeState.STUCK
    # 10 min, threshold 30 → QUEUED
    assert derive_badge_state(
        s, False, NOW, stuck_threshold_minutes=30
    ) == BadgeState.QUEUED


# ----- intake_ts edge cases -----


def test_intake_empty_ts_is_queued_not_stuck():
    """No timestamp on the row → no escalation. Pre-migration rows lack
    intake_ts; they shouldn't all flip to STUCK on the first render."""
    s = _state(Stage.intake, intake_ts="")
    assert derive_badge_state(s, False, NOW) == BadgeState.QUEUED


def test_intake_missing_ts_key_is_queued():
    """Defensive — even if the dict somehow lacks the key entirely,
    don't crash, don't escalate."""
    s = {"ucd_fil_id": "UCD-FIL-001", "stage": Stage.intake}
    assert derive_badge_state(s, False, NOW) == BadgeState.QUEUED


def test_intake_unparseable_ts_is_queued():
    """Garbage in intake_ts (shouldn't happen — schema validates — but
    defense in depth): no escalation."""
    s = _state(Stage.intake, intake_ts="not-a-timestamp")
    assert derive_badge_state(s, False, NOW) == BadgeState.QUEUED


def test_intake_naive_ts_treated_as_utc():
    """Production writes tz-aware UTC strings, but a future migration or
    operator-edit might land a naive timestamp. Treat as UTC rather than
    raising on the subtraction."""
    naive = (NOW.replace(tzinfo=None) - timedelta(minutes=30)).isoformat()
    s = _state(Stage.intake, intake_ts=naive)
    assert derive_badge_state(s, False, NOW) == BadgeState.STUCK


# ----- has_attention only matters for verified -----


def test_has_attention_does_not_override_failed():
    """A failed case with an attention item is still FAILED — the badge
    reflects the pipeline outcome, not the attention queue."""
    s = _state(Stage.failed)
    assert derive_badge_state(s, True, NOW) == BadgeState.FAILED


def test_has_attention_does_not_override_processing():
    """Open attention items shouldn't make an in-flight case look ready
    for review."""
    s = _state(Stage.deidentified)
    assert derive_badge_state(s, True, NOW) == BadgeState.PROCESSING


def test_has_attention_does_not_override_intake_stuck():
    s = _state(
        Stage.intake,
        intake_ts=(NOW - timedelta(minutes=30)).isoformat(),
    )
    assert derive_badge_state(s, True, NOW) == BadgeState.STUCK


# ----- BadgeState enum surface -----


@pytest.mark.parametrize(
    "member,value",
    [
        (BadgeState.QUEUED, "queued"),
        (BadgeState.PROCESSING, "processing"),
        (BadgeState.COMPLETE, "complete"),
        (BadgeState.FLAGGED, "flagged"),
        (BadgeState.FAILED, "failed"),
        (BadgeState.STUCK, "stuck"),
    ],
)
def test_badge_state_string_values(member, value):
    """String values are the exposed surface — Brief #2 will read them
    for HTML class names. Lock them down."""
    assert member.value == value
    assert str(member) == value
