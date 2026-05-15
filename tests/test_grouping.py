"""Tests for ``pipeline.grouping`` — the single source of truth for the
time-proximity grouping threshold used by the Intake form (Section 1) and
any future pipeline-side code that needs to reconstruct case boundaries."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.grouping import GROUP_GAP_THRESHOLD_SECONDS, group_segments


@dataclass(frozen=True)
class _Stub:
    """Minimal segment-like object — has the .timestamp the grouper needs."""

    timestamp: datetime
    label: str = ""


_T0 = datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)


def _stubs(*gaps_minutes: float) -> list[_Stub]:
    """Build a sequence of stubs starting at _T0, then advancing by each gap
    in minutes. ``_stubs()`` → 1 segment; ``_stubs(30, 70)`` → 3 segments."""
    segs = [_Stub(_T0, label="seg0")]
    cur = _T0
    for i, g in enumerate(gaps_minutes, start=1):
        cur = cur + timedelta(minutes=g)
        segs.append(_Stub(cur, label=f"seg{i}"))
    return segs


# ----- threshold constant -----


def test_threshold_constant_is_60_minutes():
    assert GROUP_GAP_THRESHOLD_SECONDS == 60 * 60


# ----- empty / single -----


def test_empty_input_returns_empty_list():
    assert group_segments([]) == []


def test_single_segment_returns_one_group_of_one():
    segs = _stubs()
    groups = group_segments(segs)
    assert len(groups) == 1
    assert groups[0].segments == tuple(segs)
    assert groups[0].start == _T0
    assert groups[0].end == _T0


# ----- threshold semantics -----


def test_gap_under_threshold_stays_in_one_group():
    """45-min gap (Spec E verification case) → 1 group."""
    segs = _stubs(45)
    groups = group_segments(segs)
    assert len(groups) == 1
    assert len(groups[0].segments) == 2


def test_gap_over_threshold_splits_into_two_groups():
    """75-min gap (Spec E verification case) → 2 groups."""
    segs = _stubs(75)
    groups = group_segments(segs)
    assert len(groups) == 2
    assert len(groups[0].segments) == 1
    assert len(groups[1].segments) == 1


def test_gap_exactly_at_threshold_stays_grouped():
    """60-min gap (boundary): the docstring promises ``>`` semantics — a
    gap of exactly the threshold value keeps segments in one group."""
    segs = _stubs(60)
    groups = group_segments(segs)
    assert len(groups) == 1


def test_gap_just_over_threshold_splits():
    segs = _stubs(60.0001)  # 60 min + a hair
    groups = group_segments(segs)
    assert len(groups) == 2


# ----- multi-case sequences -----


def test_two_cases_in_a_day_split_into_two_groups():
    """Morning case (3 segments, all <60min apart) + afternoon case
    (2 segments) separated by a 120-min lunch gap."""
    segs = _stubs(20, 20, 120, 30)
    groups = group_segments(segs)
    assert [len(g.segments) for g in groups] == [3, 2]


def test_three_cases_with_two_long_gaps():
    segs = _stubs(15, 90, 20, 90, 15, 15)
    groups = group_segments(segs)
    assert [len(g.segments) for g in groups] == [2, 2, 3]


# ----- order independence -----


def test_shuffled_input_produces_same_grouping():
    segs = _stubs(20, 20, 90, 30)
    shuffled = list(segs)
    random.Random(42).shuffle(shuffled)
    g1 = group_segments(segs)
    g2 = group_segments(shuffled)
    assert [len(g.segments) for g in g1] == [len(g.segments) for g in g2]
    # And within each group, segments are sorted ascending.
    for g in g2:
        ts_list = [s.timestamp for s in g.segments]
        assert ts_list == sorted(ts_list)


# ----- group metadata -----


def test_group_start_and_end_set_to_bounding_timestamps():
    segs = _stubs(20, 20, 90, 30)  # (3 segs) + (2 segs)
    groups = group_segments(segs)
    g1, g2 = groups
    assert g1.start == g1.segments[0].timestamp
    assert g1.end == g1.segments[-1].timestamp
    assert g2.start == g2.segments[0].timestamp
    assert g2.end == g2.segments[-1].timestamp


def test_all_input_segments_present_in_output():
    """No segment dropped or duplicated regardless of how many groups."""
    segs = _stubs(20, 90, 20, 90, 20, 90)
    groups = group_segments(segs)
    flat = [s for g in groups for s in g.segments]
    assert len(flat) == len(segs)
    assert {id(s) for s in flat} == {id(s) for s in segs}


def test_group_segments_tuple_not_list():
    """Frozen dataclass invariant — callers can't mutate group contents."""
    groups = group_segments(_stubs(20))
    assert isinstance(groups[0].segments, tuple)
