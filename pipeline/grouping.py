"""Time-proximity grouping for BDV raw segments.

Single source of truth for the auto-group threshold used by the surgeon
Intake form (Section 1) AND by future pipeline-side code that needs to
reconstruct case boundaries from a raw-segment listing. Drift between the
two would be a silent correctness bug — anything that wants to group
segments imports ``group_segments`` and ``GROUP_GAP_THRESHOLD_SECONDS``
from here.

A new group starts whenever the gap between consecutive segments (ordered
by timestamp) strictly exceeds ``GROUP_GAP_THRESHOLD_SECONDS``. Boundary:
a gap of exactly the threshold value keeps segments in the same group.

Input is order-independent — segments are sorted by ``.timestamp`` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol, Sequence

# 60 minutes. Per v18 the OR-team boundary between consecutive cases on the
# same day is reliably wider than 60 minutes (clean-up + turnover); a gap of
# <60 minutes virtually always belongs to the same case (mid-case pause,
# segment rotation). 60 minutes satisfies the Spec E verification cases
# (45-min gap stays grouped, 75-min gap splits).
GROUP_GAP_THRESHOLD_SECONDS = 3600


class _HasTimestamp(Protocol):
    timestamp: datetime


@dataclass(frozen=True)
class SegmentGroup:
    """A time-proximity cluster of segments.

    ``segments`` is a tuple ordered by timestamp ascending. ``start`` /
    ``end`` are the bounding timestamps (==first.timestamp / last.timestamp).
    """

    segments: tuple
    start: datetime
    end: datetime


def group_segments(segments: Iterable[_HasTimestamp]) -> list[SegmentGroup]:
    """Group segments by time proximity. See module docstring for semantics."""
    ordered: Sequence = sorted(segments, key=lambda s: s.timestamp)
    if not ordered:
        return []

    groups: list[SegmentGroup] = []
    current: list = [ordered[0]]
    for seg in ordered[1:]:
        gap = (seg.timestamp - current[-1].timestamp).total_seconds()
        if gap > GROUP_GAP_THRESHOLD_SECONDS:
            groups.append(_finalize(current))
            current = [seg]
        else:
            current.append(seg)
    groups.append(_finalize(current))
    return groups


def _finalize(segs: list) -> SegmentGroup:
    return SegmentGroup(
        segments=tuple(segs),
        start=segs[0].timestamp,
        end=segs[-1].timestamp,
    )
