"""Badge derivation for the surgeon "My Cases" view.

Single pure function maps a (state_row, attention_flag, now) tuple to one
of the six :class:`BadgeState` literals. No I/O, no env reads, no clock
reads — caller is responsible for assembling the inputs.

Decision table (first match wins):

    state is None                                                      → QUEUED
    stage == failed                                                    → FAILED
    stage == verified  and  has_attention                              → FLAGGED
    stage == verified                                                  → COMPLETE
    stage in {concatenated, deidentified}                              → PROCESSING
    stage == intake  and  intake_ts parseable  and  age >= threshold   → STUCK
    stage == intake  (otherwise — no/unparseable ts, or age < threshold) → QUEUED

The threshold (default 15 min) is a function argument, not an env read,
so this module stays pure and testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pipeline.schemas import Stage


class BadgeState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FLAGGED = "flagged"
    FAILED = "failed"
    STUCK = "stuck"


_PROCESSING_STAGES: frozenset[Stage] = frozenset(
    {Stage.concatenated, Stage.deidentified}
)


def _parse_iso_or_none(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Treat naive timestamps as UTC. Production writers (CaseRepository
    # submit_case → marker → dispatch.ensure_intake_row) all produce
    # tz-aware UTC strings; tolerating naive avoids a future drift footgun.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def derive_badge_state(
    state: dict | None,
    has_attention: bool,
    now: datetime,
    stuck_threshold_minutes: int = 15,
) -> BadgeState:
    """Pure function — see module docstring for the decision table.

    ``state`` is the dict shape returned by ``PipelineStateRepository``
    (``None`` if no pipeline_state row exists yet for the case).
    ``has_attention`` is a per-case boolean derived elsewhere (any open
    attention_items row for this case → ``True``). ``now`` is the caller's
    clock; ``stuck_threshold_minutes`` is the caller's policy."""
    if state is None:
        return BadgeState.QUEUED

    stage = state.get("stage")

    if stage == Stage.failed:
        return BadgeState.FAILED

    if stage == Stage.verified:
        return BadgeState.FLAGGED if has_attention else BadgeState.COMPLETE

    if stage in _PROCESSING_STAGES:
        return BadgeState.PROCESSING

    if stage == Stage.intake:
        intake_ts = _parse_iso_or_none(state.get("intake_ts"))
        if intake_ts is None:
            return BadgeState.QUEUED
        # Normalize ``now`` to UTC if naive — same drift defense as above.
        anchor = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        age_minutes = (anchor - intake_ts).total_seconds() / 60.0
        if age_minutes >= stuck_threshold_minutes:
            return BadgeState.STUCK
        return BadgeState.QUEUED

    # Unknown stage value — degrade safely to QUEUED rather than crashing
    # the My Cases render. Should never happen; future Stage additions
    # should land here as an explicit branch.
    return BadgeState.QUEUED
