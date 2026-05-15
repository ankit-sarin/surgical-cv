"""Surgeon Gradio Blocks app.

Intake tab hosts Sections 1-4 (segments, procedure + approach, case context,
notes). Section 5 (review + submit) lands in a future spec.
My Cases and Action Required remain placeholders until their own specs.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import gradio as gr

from app.auth import (
    SESSION_COOKIE_NAME,
    decode_session,
    identity_string_for_request,
    lookup_active_user,
)
from app.phi import scan_for_phi
from app.repos import (
    CsvCaseRepository,
    FilesystemRawSegmentRepository,
    PicklistValue,
    Repos,
    SegmentRecord,
    SqlitePicklistRepository,
)
from app.scopes import SurgeonScope
from pipeline.grouping import group_segments


_EMPTY_STATE_MSG = (
    "**No segments found.** Drop video files into N:\\ via Citrix → H:\\ → N:\\ "
    "to begin, then hit Refresh."
)

# Sentinel for the conversion_target_state when the "Converted" checkbox is
# on but no target approach is picked yet. ``None`` = unchecked; the empty
# string = checked-no-target; any other string = the chosen target approach.
# Section 5 will treat None as "not a conversion case", empty as a hard
# validation error, anything else as the conversion target.
_CONV_PENDING = ""


def _identity(request: gr.Request) -> str:
    return identity_string_for_request(request)


# ----- formatting helpers -----


def _fmt_size(n_bytes: int) -> str:
    n: float = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_segment_time(ts: datetime, now: datetime | None = None) -> str:
    """HH:MM if today, MMM DD HH:MM if same year, else YYYY-MM-DD HH:MM."""
    now = now or datetime.now(ts.tzinfo)
    if ts.date() == now.date():
        return ts.strftime("%H:%M")
    if ts.year == now.year:
        return ts.strftime("%b %d %H:%M")
    return ts.strftime("%Y-%m-%d %H:%M")


def fmt_segment_label(seg: SegmentRecord, now: datetime | None = None) -> str:
    return f"{_fmt_segment_time(seg.timestamp, now=now)}  ·  {seg.filename}  ·  {_fmt_size(seg.size_bytes)}"


def fmt_group_header(group, now: datetime | None = None) -> str:
    count = len(group.segments)
    total_bytes = sum(s.size_bytes for s in group.segments)
    now = now or datetime.now(group.start.tzinfo)
    if group.start.year == now.year:
        date_str = group.start.strftime("%a %b %d")
    else:
        date_str = group.start.strftime("%Y-%m-%d")
    seg_word = "segment" if count == 1 else "segments"
    return f"{date_str}  ·  {count} {seg_word}  ·  {_fmt_size(total_bytes)}"


# ----- per-request scope construction -----


def _scope_from_request(request: gr.Request | None) -> SurgeonScope | None:
    if request is None:
        return None
    cookies = getattr(request, "cookies", None) or {}
    username = decode_session(cookies.get(SESSION_COOKIE_NAME))
    if not username:
        return None
    user = lookup_active_user(username)
    if user is None or user["role"] != "surgeon":
        return None
    repos = Repos(
        case=CsvCaseRepository(),
        segment=FilesystemRawSegmentRepository(),
        picklist=SqlitePicklistRepository(),
    )
    return SurgeonScope(
        user["username"],
        user["folder_slug"],
        repos,
        specialty=user.get("specialty"),
    )


def fetch_segments(request: gr.Request) -> list[SegmentRecord]:
    scope = _scope_from_request(request)
    if scope is None:
        return []
    return list(scope.list_raw_segments())


_PICKLIST_FIELDS_FOR_INTAKE = ("procedure", "approach", "case_year", "indication")


def fetch_picklists(request: gr.Request) -> dict[str, list[PicklistValue]]:
    """Pull dropdown / radio choices for all Intake-form picklist fields.

    Per Spec G: picklist values aren't surgeon-authorization-scoped, only
    specialty-scoped. We hit ``scope.repos.picklist.list_active`` directly
    rather than via a scope method — the scope surface stays focused on
    case authorization."""
    scope = _scope_from_request(request)
    if scope is None:
        return {field: [] for field in _PICKLIST_FIELDS_FOR_INTAKE}
    return {
        field: scope.repos.picklist.list_active(field, scope.specialty)
        for field in _PICKLIST_FIELDS_FOR_INTAKE
    }


def _picklist_choices(values: list[PicklistValue]) -> list:
    """Gradio Dropdown / Radio choices: (display_label, value) tuples,
    preserving the repo-sorted ordering."""
    return [(v.display_label, v.value) for v in values]


# ----- Intake Section 1: segment selection -----


def _build_intake_section1(parent: gr.Blocks, segments_state, selected_state):
    """Render the segment-selection Section 1 inside the active Tab context."""
    gr.Markdown("### Section 1 — Raw segments")
    gr.Markdown(
        "Segments are auto-grouped by time proximity (1-hour gap = new "
        "group). Uncheck any segments that don't belong to the case "
        "you're submitting; check across groups if the auto-grouping "
        "merged or split incorrectly."
    )

    show_more_state = gr.State(False)

    with gr.Row():
        refresh_btn = gr.Button("Refresh", variant="secondary", size="sm")

    @gr.render(inputs=[segments_state, show_more_state])
    def render_sections(segments, show_more):
        if not segments:
            gr.Markdown(_EMPTY_STATE_MSG)
            return

        groups = group_segments(segments)
        groups = sorted(groups, key=lambda g: g.start, reverse=True)

        visible = groups if (show_more or len(groups) <= 3) else groups[:3]
        for i, group in enumerate(visible):
            with gr.Accordion(
                label=fmt_group_header(group), open=(i < 3)
            ):
                for seg in sorted(group.segments, key=lambda s: s.timestamp):
                    cb = gr.Checkbox(
                        label=fmt_segment_label(seg),
                        value=True,
                    )

                    def _toggle(checked, current, fn=seg.filename):
                        s = set(current)
                        if checked:
                            s.add(fn)
                        else:
                            s.discard(fn)
                        return sorted(s)

                    cb.change(
                        _toggle,
                        inputs=[cb, selected_state],
                        outputs=selected_state,
                    )

        if not show_more and len(groups) > 3:
            more = gr.Button(
                f"Show {len(groups) - 3} older group"
                f"{'s' if len(groups) - 3 != 1 else ''}",
                variant="secondary",
                size="sm",
            )
            more.click(lambda: True, None, show_more_state)

    def _initial_selection(segments: list[SegmentRecord]) -> list[str]:
        return sorted(s.filename for s in segments)

    parent.load(fetch_segments, None, segments_state)
    parent.load(_initial_selection, segments_state, selected_state)

    refresh_btn.click(fetch_segments, None, segments_state).then(
        _initial_selection, segments_state, selected_state
    )


# ----- Intake Section 2: procedure + approach -----


def _find_duplicates(primary: str | None, additionals: list) -> list[str]:
    """Return the list of procedure values that appear more than once across
    primary + additionals. Empty/None slots are ignored."""
    selected = [primary] if primary else []
    selected.extend(a for a in additionals if a)
    counts = Counter(selected)
    return sorted(v for v, c in counts.items() if c > 1)


def _build_intake_section2(
    parent: gr.Blocks,
    picklists_state,
    procedure_primary_state,
    procedure_additional_state,
    approach_state,
    conversion_target_state,
):
    gr.Markdown("### Section 2 — Procedure + approach")

    # ----- procedure block (primary + additionals) -----

    @gr.render(
        inputs=[
            picklists_state,
            procedure_primary_state,
            procedure_additional_state,
        ]
    )
    def render_procedures(picklists, primary, additionals):
        proc_choices = _picklist_choices(picklists.get("procedure", []))

        primary_dd = gr.Dropdown(
            label="Primary procedure",
            choices=proc_choices,
            value=primary,
            filterable=True,
            allow_custom_value=False,
        )
        primary_dd.change(
            lambda v: v, inputs=primary_dd, outputs=procedure_primary_state
        )

        if additionals:
            gr.Markdown("**Additional procedures**")
        for idx, val in enumerate(additionals):
            with gr.Row():
                add_dd = gr.Dropdown(
                    label=f"Additional #{idx + 1}",
                    choices=proc_choices,
                    value=val,
                    filterable=True,
                    allow_custom_value=False,
                    scale=4,
                )
                rm_btn = gr.Button("✕", scale=1, size="sm")

                def _update_slot(new_val, current, i=idx):
                    out = list(current)
                    if i < len(out):
                        out[i] = new_val
                    return out

                def _remove_slot(current, i=idx):
                    return [v for j, v in enumerate(current) if j != i]

                add_dd.change(
                    _update_slot,
                    inputs=[add_dd, procedure_additional_state],
                    outputs=procedure_additional_state,
                )
                rm_btn.click(
                    _remove_slot,
                    inputs=procedure_additional_state,
                    outputs=procedure_additional_state,
                )

        add_btn = gr.Button(
            "+ Add additional procedure", variant="secondary", size="sm"
        )
        add_btn.click(
            lambda current: list(current) + [None],
            inputs=procedure_additional_state,
            outputs=procedure_additional_state,
        )

        dupes = _find_duplicates(primary, additionals)
        if dupes:
            gr.Markdown(
                f"⚠️ Duplicate procedure selection: **{', '.join(dupes)}**. "
                "Each procedure can appear once per case."
            )

    # ----- approach block (primary + optional conversion) -----

    @gr.render(
        inputs=[
            picklists_state,
            approach_state,
            conversion_target_state,
        ]
    )
    def render_approach(picklists, approach, conv_target):
        approach_values = picklists.get("approach", [])
        approach_options = [v.value for v in approach_values]

        primary_radio = gr.Radio(
            label="Primary approach",
            choices=approach_options,
            value=approach,
        )
        primary_radio.change(
            lambda v: v, inputs=primary_radio, outputs=approach_state
        )

        converted = conv_target is not None
        conv_cb = gr.Checkbox(
            label="Converted to a different approach",
            value=converted,
        )

        def _toggle_conversion(checked, current):
            if checked:
                # Preserve any target the user previously picked; otherwise
                # transition to the "checked-no-target" sentinel.
                return current if current is not None else _CONV_PENDING
            # Unchecked → clear target.
            return None

        conv_cb.change(
            _toggle_conversion,
            inputs=[conv_cb, conversion_target_state],
            outputs=conversion_target_state,
        )

        if converted:
            target_radio = gr.Radio(
                label="Conversion target",
                choices=approach_options,
                value=conv_target if conv_target else None,
            )
            target_radio.change(
                lambda v: v,
                inputs=target_radio,
                outputs=conversion_target_state,
            )

            if conv_target and approach and conv_target == approach:
                gr.Markdown(
                    "⚠️ Conversion target matches the primary approach. "
                    "Pick a different target or uncheck Converted."
                )


# ----- Intake Section 3: case context (case_year, or_room, indication) -----


_OR_ROOM_PLACEHOLDER = "e.g., OR 4, ASC OR 2, Hybrid OR 3"
_OR_ROOM_MAX_LENGTH = 50


def _normalize_or_room(value: str | None) -> str | None:
    """Trim whitespace; treat empty / whitespace-only as ``None``. Mirrors
    the Section 5 submit-time validation rule (non-empty, 50-char cap)
    in a forgiving way at edit time."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _build_intake_section3(
    parent: gr.Blocks,
    picklists_state,
    case_year_state,
    or_room_state,
    indication_state,
):
    gr.Markdown("### Section 3 — Case context")

    # or_room lives OUTSIDE the @gr.render so user keystrokes don't trigger
    # a rebuild of the section (which would steal focus from the input).
    or_tb = gr.Textbox(
        label="OR room",
        placeholder=_OR_ROOM_PLACEHOLDER,
        max_lines=1,
        max_length=_OR_ROOM_MAX_LENGTH,
    )
    or_tb.blur(
        _normalize_or_room, inputs=or_tb, outputs=or_room_state
    )

    @gr.render(inputs=[picklists_state, case_year_state, indication_state])
    def render_section3_dropdowns(picklists, case_year, indication):
        cy_choices = _picklist_choices(picklists.get("case_year", []))
        cy_dd = gr.Dropdown(
            label="Case year",
            choices=cy_choices,
            value=case_year,
            filterable=True,
            allow_custom_value=False,
        )
        cy_dd.change(lambda v: v, inputs=cy_dd, outputs=case_year_state)

        ind_choices = _picklist_choices(picklists.get("indication", []))
        ind_dd = gr.Dropdown(
            label="Indication",
            choices=ind_choices,
            value=indication,
            filterable=True,
            allow_custom_value=False,
        )
        ind_dd.change(lambda v: v, inputs=ind_dd, outputs=indication_state)


# ----- Intake Section 4: notes (with soft PHI warning) -----


_NOTES_PLACEHOLDER = (
    "Optional. Avoid PHI (names, MRNs, SSNs, specific dates)."
)
_NOTES_SOFT_LIMIT = 500
_NOTES_HARD_LIMIT = 1000

# Co-located with scan_for_phi's categories. Humanized for the warning UI.
# Privacy: these labels never accompany the matched substring — counts only.
_PHI_CATEGORY_LABELS = {
    "mrn": "long numbers",
    "ssn": "SSN-like format",
    "date": "dates",
}


def _normalize_notes(value: str | None) -> str | None:
    """Trim; empty / whitespace-only collapses to ``None`` (case has no notes)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _format_notes_counter(length: int) -> str:
    """Neutral when under the 500-char soft limit, amber (warning glyph)
    when 500-1000. The hard cap is always shown so the user knows the
    ceiling Gradio is enforcing at input level."""
    if length >= _NOTES_SOFT_LIMIT:
        return f"⚠ {length} / {_NOTES_HARD_LIMIT} characters"
    return f"{length} / {_NOTES_HARD_LIMIT} characters"


def _format_phi_warning(text: str | None) -> str:
    """Render the soft PHI warning Markdown. Returns the empty string when
    no PHI patterns matched (caller surface stays blank in the clean case)."""
    matches = scan_for_phi(text or "")
    if not matches:
        return ""
    parts = []
    for category, label in _PHI_CATEGORY_LABELS.items():
        count = matches.get(category)
        if count:
            parts.append(f"{label} ({count})")
    return (
        f"⚠ Possible PHI detected: {', '.join(parts)}. "
        "You'll be asked to confirm at submission."
    )


def _build_intake_section4(
    parent: gr.Blocks,
    notes_state,
    notes_phi_warnings_state,
):
    gr.Markdown("### Section 4 — Notes")

    # Static textbox (same focus-preservation pattern as Section 3's or_room):
    # lives outside @gr.render so keystrokes don't rebuild the section.
    notes_tb = gr.Textbox(
        label="Case notes",
        placeholder=_NOTES_PLACEHOLDER,
        lines=6,
        max_length=_NOTES_HARD_LIMIT,
    )
    counter_md = gr.Markdown(_format_notes_counter(0))
    phi_warning_md = gr.Markdown("")

    # All four wirings on .blur — single round-trip per defocus rather than
    # one per keystroke.
    notes_tb.blur(_normalize_notes, inputs=notes_tb, outputs=notes_state)
    notes_tb.blur(
        scan_for_phi, inputs=notes_tb, outputs=notes_phi_warnings_state
    )
    notes_tb.blur(_format_phi_warning, inputs=notes_tb, outputs=phi_warning_md)
    notes_tb.blur(
        lambda v: _format_notes_counter(len(v) if v else 0),
        inputs=notes_tb,
        outputs=counter_md,
    )


# ----- top-level Blocks build -----


def build_surgeon_app() -> gr.Blocks:
    with gr.Blocks(
        title="Surgeon — surgical-cv", analytics_enabled=False
    ) as blocks:
        identity_md = gr.Markdown()
        with gr.Tabs():
            with gr.Tab("Intake"):
                # Shared state across sections — Section 5 will consume all
                # of them at submit time.
                segments_state = gr.State([])
                selected_state = gr.State([])
                picklists_state = gr.State(
                    {field: [] for field in _PICKLIST_FIELDS_FOR_INTAKE}
                )
                procedure_primary_state = gr.State(None)
                procedure_additional_state = gr.State([])
                approach_state = gr.State(None)
                conversion_target_state = gr.State(None)
                case_year_state = gr.State(None)
                or_room_state = gr.State(None)
                indication_state = gr.State(None)
                notes_state = gr.State(None)
                notes_phi_warnings_state = gr.State({})

                _build_intake_section1(
                    blocks, segments_state, selected_state
                )
                _build_intake_section2(
                    blocks,
                    picklists_state,
                    procedure_primary_state,
                    procedure_additional_state,
                    approach_state,
                    conversion_target_state,
                )
                _build_intake_section3(
                    blocks,
                    picklists_state,
                    case_year_state,
                    or_room_state,
                    indication_state,
                )
                _build_intake_section4(
                    blocks, notes_state, notes_phi_warnings_state
                )

                blocks.load(fetch_picklists, None, picklists_state)
            with gr.Tab("My Cases"):
                gr.Markdown("**My Cases** — coming soon.")
            with gr.Tab("Action Required"):
                gr.Markdown("**Action Required** — coming soon.")
        blocks.load(_identity, None, identity_md)
    return blocks
