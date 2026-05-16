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
#
# This constant is loaded into the surgeon Blocks via ``gr.Blocks(css=...)``
# rather than a hidden ``gr.HTML`` so the rules definitely apply — Gradio
# wraps each component in a div whose ``visible=False`` may cascade to
# child <style> tags depending on Svelte's render path. The Blocks-level
# css= is the sanctioned channel.
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

/* Pipeline timeline — inline SVG.
   Renders identically across all browsers regardless of CSS box-model
   quirks; CSS classes on <g>/<circle> drive per-step state colors so
   markup stays semantic and theme-tokenized. */
.ds-timeline-svg {
  display: block;
  max-width: 480px;
  margin: 8px 0;
  font-family: 'IBM Plex Sans', sans-serif;
}
.ds-timeline-svg .ds-timeline-line {
  stroke: var(--ds-border);
  stroke-width: 2;
}
.ds-timeline-svg .ds-timeline-dot-circle {
  fill: var(--ds-bg);
  stroke: var(--ds-border);
  stroke-width: 2;
}
.ds-timeline-svg .ds-timeline-step.is-filled .ds-timeline-dot-circle {
  fill: var(--ds-success);
  stroke: var(--ds-success);
}
.ds-timeline-svg .ds-timeline-step.is-current .ds-timeline-dot-circle {
  fill: var(--ds-primary);
  stroke: var(--ds-primary);
}
.ds-timeline-svg .ds-timeline-step.is-failed .ds-timeline-dot-circle {
  fill: var(--ds-error);
  stroke: var(--ds-error);
}
.ds-timeline-svg .ds-timeline-step.is-stuck .ds-timeline-dot-circle {
  fill: transparent;
  stroke: var(--ds-warning);
}
.ds-timeline-svg .ds-timeline-label {
  font-size: 11px;
  fill: var(--ds-text-muted);
  font-family: 'IBM Plex Sans', sans-serif;
}
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


# SVG geometry: viewBox 480 wide, 50 tall; 4 evenly-spaced steps with
# 30 px side margin so the dots have room.
_SVG_VIEWBOX = (480, 50)
_SVG_DOT_RADIUS = 9
_SVG_DOT_Y = 15
_SVG_LABEL_Y = 42
_SVG_SIDE_MARGIN = 30


def _svg_step_x_coords() -> list[int]:
    """Even spacing across viewBox width, accounting for side margin."""
    n = len(_TIMELINE_STEP_LABELS)
    width = _SVG_VIEWBOX[0]
    span = width - 2 * _SVG_SIDE_MARGIN
    return [
        _SVG_SIDE_MARGIN + (span * i // (n - 1)) for i in range(n)
    ]


def pipeline_timeline_html(state: dict | None, badge: BadgeState) -> str:
    """Render the 4-step horizontal pipeline timeline as inline SVG.

    Pure function; ``state`` is the dict shape from PipelineStateRepository
    (or None for queued / not-yet-dispatched cases). Inline SVG instead of
    div+span because (a) the brief acceptance asks for ``<svg`` in the
    rendered output, and (b) SVG renders identically across browsers
    regardless of column width or CSS quirks — important on the OR's
    narrow Citrix viewports."""
    xs = _svg_step_x_coords()
    width, height = _SVG_VIEWBOX

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" '
        f'class="ds-timeline-svg" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Pipeline progress">'
    ]

    # Connecting line — runs through the centers of all dots.
    parts.append(
        f'<line class="ds-timeline-line" '
        f'x1="{xs[0]}" y1="{_SVG_DOT_Y}" '
        f'x2="{xs[-1]}" y2="{_SVG_DOT_Y}" />'
    )

    # Per-step group: dot + label, with the state class on the group so
    # CSS rules can target either piece via the parent.
    for i, label in enumerate(_TIMELINE_STEP_LABELS):
        cls = _step_class_for(i, state, badge)
        parts.append(
            f'<g class="ds-timeline-step{cls}" data-step="{i}">'
            f'<circle class="ds-timeline-dot-circle" '
            f'cx="{xs[i]}" cy="{_SVG_DOT_Y}" r="{_SVG_DOT_RADIUS}" />'
            f'<text class="ds-timeline-label" '
            f'x="{xs[i]}" y="{_SVG_LABEL_Y}" '
            f'text-anchor="middle">{escape(label)}</text>'
            f'</g>'
        )
    parts.append('</svg>')
    return "".join(parts)


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
