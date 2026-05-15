"""Pure HTML helpers for the surgeon "My Cases" tab — badges, pipeline
timeline, header counter strip, footer.

No I/O, no clock reads, no env reads. Caller assembles the inputs.

Styling tokens come from the digitalsurgeon-brand skill — every CSS class
emitted here is defined in ``~/.claude/skills/digitalsurgeon-brand/
assets/gradio-theme.css``. The :data:`MY_CASES_CSS` constant inlines a
project-local copy of the badge / timeline rules so the surgeon app does
not pick up a runtime dependency on the skill directory; if the brand
skill's badge/timeline rules change, sync them here.

Tests assert on semantic class names and ``data-badge="<state>"`` /
``aria-label`` attributes — never on hex values, which belong to the
brand skill.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from app.badges import BadgeState


# Synced from ~/.claude/skills/digitalsurgeon-brand/assets/gradio-theme.css
# (badge + timeline rules only, plus state-token fallbacks so the page
# renders correctly even if the full brand theme is not loaded).
MY_CASES_CSS = """
:root {
  --ds-primary: #0A5E56;
  --ds-accent: #B85D3A;
  --ds-text: #2C2C2C;
  --ds-text-muted: #6B6B6B;
  --ds-border: #C5CDD6;
  --ds-bg: #EEF5F4;
  --ds-success: #2D7A52;
  --ds-warning: #C57E1E;
  --ds-error: var(--ds-accent);
}
.ds-badge {
  display: inline-block;
  font-family: 'IBM Plex Sans', sans-serif;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.02em;
  padding: 2px 10px;
  border-radius: 999px;
  border: 1px solid transparent;
  white-space: nowrap;
  line-height: 1.4;
}
.ds-badge-queued { background-color: var(--ds-border); color: var(--ds-text); }
.ds-badge-processing { background-color: var(--ds-primary); color: #ffffff; }
.ds-badge-complete { background-color: var(--ds-success); color: #ffffff; }
.ds-badge-flagged { background-color: var(--ds-warning); color: #ffffff; }
.ds-badge-failed { background-color: var(--ds-error); color: #ffffff; }
.ds-badge-stuck {
  background-color: transparent;
  color: var(--ds-warning);
  border-color: var(--ds-warning);
}
.ds-timeline {
  display: flex; align-items: flex-start;
  font-family: 'IBM Plex Sans', sans-serif;
  font-size: 12px; color: var(--ds-text-muted);
  margin: 8px 0;
}
.ds-timeline-step { flex: 1; text-align: center; position: relative; }
.ds-timeline-step + .ds-timeline-step::before {
  content: ''; position: absolute; top: 9px; left: -50%;
  width: 100%; height: 2px; background-color: var(--ds-border);
}
.ds-timeline-dot {
  display: inline-block; width: 18px; height: 18px;
  border-radius: 50%; background-color: var(--ds-bg);
  border: 2px solid var(--ds-border);
  margin-bottom: 4px; position: relative; z-index: 1;
}
.ds-timeline-step.is-filled .ds-timeline-dot {
  background-color: var(--ds-success); border-color: var(--ds-success);
}
.ds-timeline-step.is-current .ds-timeline-dot {
  background-color: var(--ds-primary); border-color: var(--ds-primary);
}
.ds-timeline-step.is-failed .ds-timeline-dot {
  background-color: var(--ds-error); border-color: var(--ds-error);
}
.ds-timeline-step.is-stuck .ds-timeline-dot {
  background-color: transparent; border-color: var(--ds-warning);
}
.ds-timeline-label { display: block; font-size: 11px; }
"""


_BADGE_LABELS: dict[BadgeState, str] = {
    BadgeState.QUEUED: "Queued",
    BadgeState.PROCESSING: "Processing",
    BadgeState.COMPLETE: "Complete",
    BadgeState.FLAGGED: "Flagged",
    BadgeState.FAILED: "Failed",
    BadgeState.STUCK: "Stuck",
}


def badge_html(badge: BadgeState) -> str:
    """Render a single status badge. Class is ``ds-badge ds-badge-<state>``;
    ``data-badge`` and ``aria-label`` carry the semantic state for both
    test introspection and assistive technology."""
    label = _BADGE_LABELS[badge]
    return (
        f'<span class="ds-badge ds-badge-{badge.value}" '
        f'data-badge="{badge.value}" aria-label="Status: {label}">'
        f'{label}'
        f'</span>'
    )


# The four pipeline timeline steps map 1:1 to the four pipeline stages a
# surgeon's case progresses through. ``Submit`` is shown filled the moment
# a state row exists (the marker landed), regardless of dispatch progress —
# from the surgeon's POV "I uploaded it" is the relevant milestone.
_TIMELINE_STEP_LABELS: tuple[str, ...] = ("Submit", "Concat", "Deid", "Verify")


# Per-step timestamp key used to decide "is this step done?". Step 0
# (Submit) has no timestamp condition — the existence of a state row
# already implies the marker landed.
_STEP_TS_KEYS: tuple[str | None, ...] = (
    None, "concat_ts", "deid_ts", "verify_ts",
)


def _step_class_for(
    step_index: int,
    state: dict | None,
    badge: BadgeState,
) -> str:
    """Decide one step's state class. ``step_index`` is 0-3 corresponding
    to Submit / Concat / Deid / Verify. Returns the trailing class string
    (e.g. ``" is-filled"``); empty string for the default "not yet" look.

    Logic:
      - Step 0 (Submit) is always satisfied when the state row exists;
        STUCK badge styles it as the warning-outline variant.
      - Steps 1-3 are satisfied iff their corresponding timestamp is set.
      - For FAILED/PROCESSING badges, the first not-yet-satisfied step
        (whose predecessor IS satisfied) gets the failed/current marker."""
    if state is None:
        return ""

    if step_index == 0:
        if badge == BadgeState.STUCK:
            return " is-stuck"
        return " is-filled"

    own_key = _STEP_TS_KEYS[step_index]
    if state.get(own_key):
        return " is-filled"

    prev_key = _STEP_TS_KEYS[step_index - 1]
    prev_satisfied = prev_key is None or bool(state.get(prev_key))
    if not prev_satisfied:
        return ""

    if badge == BadgeState.FAILED:
        return " is-failed"
    if badge == BadgeState.PROCESSING:
        return " is-current"
    return ""


def pipeline_timeline_html(state: dict | None, badge: BadgeState) -> str:
    """Render the 4-step horizontal timeline. Pure function; ``state`` is
    the dict shape from PipelineStateRepository (or None for queued/
    not-yet-dispatched cases)."""
    steps_html = []
    for i, label in enumerate(_TIMELINE_STEP_LABELS):
        cls = _step_class_for(i, state, badge)
        steps_html.append(
            f'<div class="ds-timeline-step{cls}" data-step="{i}">'
            f'<span class="ds-timeline-dot"></span>'
            f'<span class="ds-timeline-label">{escape(label)}</span>'
            f'</div>'
        )
    return f'<div class="ds-timeline">{"".join(steps_html)}</div>'


def format_counter_strip(counts: dict[BadgeState, int]) -> str:
    """Surgeon-facing header line. Buckets:
        - Complete   = COMPLETE badges
        - In progress = PROCESSING + QUEUED + STUCK (anything mid-flight)
        - Need attention = FLAGGED + FAILED (anything terminal-but-not-clean)
    Format: ``"N cases · X complete · Y in progress · Z need attention"``.
    Singular/plural always says "cases" — copy stays terse and consistent."""
    complete = counts.get(BadgeState.COMPLETE, 0)
    in_progress = (
        counts.get(BadgeState.PROCESSING, 0)
        + counts.get(BadgeState.QUEUED, 0)
        + counts.get(BadgeState.STUCK, 0)
    )
    need_attention = (
        counts.get(BadgeState.FLAGGED, 0)
        + counts.get(BadgeState.FAILED, 0)
    )
    total = complete + in_progress + need_attention
    return (
        f"{total} cases · {complete} complete · "
        f"{in_progress} in progress · {need_attention} need attention"
    )


def format_footer(now: datetime) -> str:
    """Auto-refresh footer line. ``now`` is the caller's clock, displayed
    as ``HH:MM:SS`` in whatever timezone the caller passed (UTC for v1)."""
    return (
        f"Auto-refreshes every 30 s · last updated {now.strftime('%H:%M:%S')}"
    )
