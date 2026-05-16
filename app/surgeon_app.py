"""Surgeon Gradio Blocks app.

Intake tab hosts Sections 1-4 (segments, procedure + approach, case context,
notes). Section 5 (review + submit) lands in a future spec.
My Cases tab is read-only status display with 30 s polling.
Action Required remains a placeholder until its own spec.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape

import gradio as gr

from app.auth import (
    SESSION_COOKIE_NAME,
    decode_session,
    identity_string_for_request,
    lookup_active_user,
)
from app.attention_actions import (
    SURGEON_ACTION_BY_TYPE,
    action_for_type,
    display_for_type,
)
from app.badges import BadgeState, derive_badge_state
from app.badges_html import (
    MY_CASES_CSS,
    badge_html,
    format_counter_strip,
    format_footer,
    pipeline_timeline_html,
)
from app.repos import (
    AttentionItem,
    AttentionItemActionMismatchError,
    AttentionItemAlreadyClosedError,
    AttentionItemNotFoundError,
)
from app.intake.submit import (
    SubmitOutcome,
    ValidationContext,
    handle_submit_request,
)
from app.phi import scan_for_phi
from app.repos import (
    CaseManifestRow,
    CsvCaseManifestRepository,
    CsvCaseRepository,
    CsvPipelineStateRepository,
    FilesystemRawSegmentRepository,
    PicklistValue,
    Repos,
    SegmentRecord,
    SqliteAttentionItemsRepository,
    SqlitePicklistRepository,
)
from app.scopes import SurgeonScope
from pipeline.grouping import group_segments


# Read once at module load; runtime reconfigurability is a future spec.
_STUCK_THRESHOLD_MINUTES = int(os.environ.get("STUCK_THRESHOLD_MINUTES", "15"))

# Brief #3.1: My Cases dropped the gr.DataFrame component to work around
# Gradio issue #12947 (a pre-Svelte-5 reactivity recursion in the Dataframe
# Svelte component's groupedColumnMode getter that hangs the surgeon's
# browser within seconds of mounting the tab — Uncaught RangeError:
# Maximum call stack size exceeded). Replaced with a pre-allocated pool
# of expandable cards, matching the Action Required idiom. Allocate
# comfortably so a busy surgeon's full corpus renders; cases beyond the
# 50th get a "more cases" footer notice when we cross that threshold.
_MAX_VISIBLE_MY_CASES_SLOTS = 50

_EMPTY_CASES_MARKDOWN = (
    "_No cases yet. Submit your first case via the Intake tab._"
)


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
        pipeline_state=CsvPipelineStateRepository(),
        attention=SqliteAttentionItemsRepository(),
        case_manifest=CsvCaseManifestRepository(),
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


def _build_intake_section1(
    parent: gr.Blocks, segments_state, selected_state, show_more_state
):
    """Render the segment-selection Section 1 inside the active Tab context."""
    gr.Markdown("### Section 1 — Raw segments")
    gr.Markdown(
        "Segments are auto-grouped by time proximity (1-hour gap = new "
        "group). Uncheck any segments that don't belong to the case "
        "you're submitting; check across groups if the auto-grouping "
        "merged or split incorrectly."
    )

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
    """Returns the stable or_room textbox handle so Section 5's reset
    can clear its DOM value (state resets alone don't propagate into
    static textboxes — that's the Spec H/I locked pattern)."""
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

    return or_tb


# ----- Intake Section 4: notes (with soft PHI warning) -----


_NOTES_PLACEHOLDER = (
    "Optional. Avoid PHI (names, MRNs, SSNs, specific dates)."
)
_NOTES_SOFT_LIMIT = 500
_NOTES_HARD_LIMIT = 1000

# Co-located with scan_for_phi's categories. Humanized for the warning UI.
# Privacy: these labels never accompany the matched substring — counts only.
# Order here drives the rendered display order in _format_phi_warning.
_PHI_CATEGORY_LABELS = {
    "mrn": "long numbers",
    "ssn": "SSN-like format",
    "date": "dates",
    "name": "names",
    "phone": "phone numbers",
    "address": "addresses",
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
    """Returns ``(notes_tb, counter_md, phi_warning_md)`` so Section 5 can
    reset all three on auto-clear (static textbox + the two derived
    markdowns)."""
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

    return notes_tb, counter_md, phi_warning_md


# ----- Intake Section 5: submit handler integration -----


def _format_success_banner(ucd_fil_id: str) -> str:
    return (
        f"✓ Case **{ucd_fil_id}** submitted. Processing typically "
        "begins within 10 minutes."
    )


# Reset value for the form-cleared notice (used by the Clear-form button).
_CLEAR_FORM_BANNER = "Form cleared."


def _empty_picklists() -> dict[str, list[PicklistValue]]:
    return {field: [] for field in _PICKLIST_FIELDS_FOR_INTAKE}


def _build_intake_section5(
    parent: gr.Blocks,
    *,
    # State seams from Sections 1-4 — full reset on success / clear.
    segments_state,
    selected_state,
    show_more_state,
    picklists_state,
    procedure_primary_state,
    procedure_additional_state,
    approach_state,
    conversion_target_state,
    case_year_state,
    or_room_state,
    indication_state,
    notes_state,
    notes_phi_warnings_state,
    # Static textboxes + derived markdowns — explicit value resets per the
    # Spec H/I locked pattern.
    or_room_tb,
    notes_tb,
    notes_counter_md,
    notes_phi_warning_md,
    # Banner above Section 1.
    success_banner_md,
):
    gr.Markdown("### Section 5 — Review and submit")

    validation_error_md = gr.Markdown("")

    with gr.Group(visible=False) as phi_confirm_group:
        gr.Markdown(
            "⚠️ **Possible PHI in notes.** Review before submitting:"
        )
        phi_confirm_message_md = gr.Markdown("")
        gr.Markdown(
            "If the notes are clean, click Confirm and submit. Otherwise "
            "cancel, edit Section 4, and resubmit."
        )
        with gr.Row():
            confirm_submit_btn = gr.Button(
                "Confirm and submit", variant="primary"
            )
            cancel_btn = gr.Button("Cancel", variant="secondary")

    with gr.Row():
        clear_btn = gr.Button(
            "Clear form", variant="secondary", size="sm"
        )
        submit_btn = gr.Button("Submit case", variant="primary")

    # Shared input list for both Submit and Confirm — keeps the two click
    # handlers (which only differ in whether they bypass the PHI gate)
    # parameter-aligned with one source of truth.
    handler_inputs = [
        segments_state,
        selected_state,
        picklists_state,
        procedure_primary_state,
        procedure_additional_state,
        approach_state,
        conversion_target_state,
        case_year_state,
        or_room_state,
        indication_state,
        notes_state,
        notes_phi_warnings_state,
    ]

    # Outputs the submit / confirm handlers must produce a tuple matching.
    handler_outputs = [
        success_banner_md,
        validation_error_md,
        phi_confirm_group,
        phi_confirm_message_md,
        # 13 State seams (excluding picklists_state — kept loaded):
        segments_state,
        selected_state,
        show_more_state,
        procedure_primary_state,
        procedure_additional_state,
        approach_state,
        conversion_target_state,
        case_year_state,
        or_room_state,
        indication_state,
        notes_state,
        notes_phi_warnings_state,
        # Static textbox values + derived markdowns:
        or_room_tb,
        notes_tb,
        notes_counter_md,
        notes_phi_warning_md,
    ]

    def _no_op_tuple(
        segments, selected, picklists, p_primary, p_additional, approach,
        conv_target, case_year, or_room, indication, notes, phi_warnings,
    ):
        """Return a tuple matching ``handler_outputs`` that leaves every
        state seam unchanged. Used for the validation-error path."""
        return (
            gr.update(value=""),                  # success_banner_md
            gr.update(),                          # validation_error_md (overwritten)
            gr.update(visible=False),             # phi_confirm_group
            gr.update(),                          # phi_confirm_message_md
            segments,
            selected,
            gr.update(),                          # show_more_state
            p_primary,
            p_additional,
            approach,
            conv_target,
            case_year,
            or_room,
            indication,
            notes,
            phi_warnings,
            gr.update(),                          # or_room_tb
            gr.update(),                          # notes_tb
            gr.update(),                          # notes_counter_md
            gr.update(),                          # notes_phi_warning_md
        )

    def _reset_tuple(banner_text: str, request: gr.Request | None = None):
        """Tuple matching ``handler_outputs`` for the full auto-clear:
        success banner set, all 13 data states reset to defaults, both
        static textboxes cleared, Section 4 markdowns reset to neutral."""
        fresh_segments = fetch_segments(request) if request else []
        return (
            gr.update(value=banner_text),         # success_banner_md
            gr.update(value=""),                  # validation_error_md
            gr.update(visible=False),             # phi_confirm_group
            gr.update(value=""),                  # phi_confirm_message_md
            fresh_segments,                       # segments_state
            sorted(s.filename for s in fresh_segments),  # selected_state
            False,                                # show_more_state
            None,                                 # procedure_primary_state
            [],                                   # procedure_additional_state
            None,                                 # approach_state
            None,                                 # conversion_target_state
            None,                                 # case_year_state
            None,                                 # or_room_state
            None,                                 # indication_state
            None,                                 # notes_state
            {},                                   # notes_phi_warnings_state
            gr.update(value=""),                  # or_room_tb (DOM clear)
            gr.update(value=""),                  # notes_tb (DOM clear)
            _format_notes_counter(0),             # notes_counter_md
            "",                                   # notes_phi_warning_md
        )

    def _dispatch_submit(
        request: gr.Request,
        segments, selected, picklists, p_primary, p_additional, approach,
        conv_target, case_year, or_room, indication, notes, phi_warnings,
        *,
        phi_already_confirmed: bool,
    ):
        scope = _scope_from_request(request)
        if scope is None:
            outputs = list(_no_op_tuple(
                segments, selected, picklists, p_primary, p_additional,
                approach, conv_target, case_year, or_room, indication,
                notes, phi_warnings,
            ))
            outputs[1] = gr.update(value=(
                "⚠️ Not signed in or session expired. Reload to log in again."
            ))
            return tuple(outputs)

        ctx = ValidationContext(
            segments_selected=selected or [],
            procedure_primary=p_primary,
            procedure_additional=p_additional or [],
            approach=approach,
            conversion_target=conv_target,
            case_year=case_year,
            or_room=or_room,
            indication=indication,
        )
        outcome: SubmitOutcome = handle_submit_request(
            surgeon=scope.folder_slug,
            ctx=ctx,
            notes=notes,
            notes_phi_warnings=phi_warnings,
            picklists=picklists or _empty_picklists(),
            segment_filenames=list(selected or []),
            submit_fn=scope.repos.case.submit_case,
            phi_already_confirmed=phi_already_confirmed,
        )

        if outcome.kind == "validation_error":
            outputs = list(_no_op_tuple(
                segments, selected, picklists, p_primary, p_additional,
                approach, conv_target, case_year, or_room, indication,
                notes, phi_warnings,
            ))
            outputs[1] = gr.update(value=outcome.error_block)
            return tuple(outputs)

        if outcome.kind == "phi_confirm":
            outputs = list(_no_op_tuple(
                segments, selected, picklists, p_primary, p_additional,
                approach, conv_target, case_year, or_room, indication,
                notes, phi_warnings,
            ))
            outputs[1] = gr.update(value="")
            outputs[2] = gr.update(visible=True)
            outputs[3] = gr.update(value=_format_phi_warning(notes))
            return tuple(outputs)

        if outcome.kind == "infra_error":
            outputs = list(_no_op_tuple(
                segments, selected, picklists, p_primary, p_additional,
                approach, conv_target, case_year, or_room, indication,
                notes, phi_warnings,
            ))
            outputs[1] = gr.update(value=(
                f"⚠️ Submission failed: {outcome.infra_error}"
            ))
            return tuple(outputs)

        # outcome.kind == "success"
        result = outcome.submit_result
        return _reset_tuple(
            _format_success_banner(result.ucd_fil_id), request=request
        )

    def _on_submit(
        request: gr.Request,
        segments, selected, picklists, p_primary, p_additional, approach,
        conv_target, case_year, or_room, indication, notes, phi_warnings,
    ):
        return _dispatch_submit(
            request, segments, selected, picklists, p_primary, p_additional,
            approach, conv_target, case_year, or_room, indication, notes,
            phi_warnings, phi_already_confirmed=False,
        )

    def _on_confirm(
        request: gr.Request,
        segments, selected, picklists, p_primary, p_additional, approach,
        conv_target, case_year, or_room, indication, notes, phi_warnings,
    ):
        return _dispatch_submit(
            request, segments, selected, picklists, p_primary, p_additional,
            approach, conv_target, case_year, or_room, indication, notes,
            phi_warnings, phi_already_confirmed=True,
        )

    def _on_cancel():
        return gr.update(visible=False)

    def _on_clear(request: gr.Request):
        return _reset_tuple(_CLEAR_FORM_BANNER, request=request)

    submit_btn.click(
        _on_submit, inputs=handler_inputs, outputs=handler_outputs
    )
    confirm_submit_btn.click(
        _on_confirm, inputs=handler_inputs, outputs=handler_outputs
    )
    cancel_btn.click(_on_cancel, outputs=phi_confirm_group)
    clear_btn.click(_on_clear, outputs=handler_outputs)

    return validation_error_md, phi_confirm_group, submit_btn, clear_btn


# ----- My Cases tab -----


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_or_none(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date_for_row(state: dict | None, case: dict) -> str:
    """``YYYY-MM-DD`` if ``intake_ts`` parses, else fall back to
    ``case_year``. UTC-displayed for v1 — local-tz conversion is a
    follow-up spec when surgeons start complaining."""
    if state is not None:
        parsed = _parse_iso_or_none(state.get("intake_ts"))
        if parsed is not None:
            return parsed.date().isoformat()
    return case.get("case_year", "")


def _updated_for_row(state: dict | None) -> str:
    """Most-recent stage timestamp on the row, displayed as ISO without
    seconds — empty for cases that never advanced past intake."""
    if state is None:
        return ""
    for key in ("verify_ts", "deid_ts", "concat_ts", "intake_ts"):
        ts = _parse_iso_or_none(state.get(key))
        if ts is not None:
            return ts.strftime("%Y-%m-%d %H:%M")
    return ""


def _sort_key(case_id: str, case: dict, state: dict | None) -> tuple:
    """Newest first by ``intake_ts`` desc; pre-migration rows (empty
    intake_ts) fall back to case_year desc, then ucd_fil_id desc.

    Returns a comparison tuple where smaller = appears earlier in the
    sorted output. We want descending order, so each tuple element is
    negated (timestamps via -timestamp(), strings via reverse()).
    Sorting ascending on the resulting tuple gives newest-first."""
    has_ts = False
    ts_key = 0.0
    if state is not None:
        parsed = _parse_iso_or_none(state.get("intake_ts"))
        if parsed is not None:
            has_ts = True
            ts_key = -parsed.timestamp()
    # Primary key: 0 if a real intake_ts exists (so timestamped rows sort
    # as a group above the legacy ones), 1 if it doesn't.
    primary = 0 if has_ts else 1
    # Within the timestamped group: -timestamp (desc by time).
    # Within the legacy group: -int(case_year) then reverse case_id.
    try:
        year_key = -int(case.get("case_year", "0"))
    except (ValueError, TypeError):
        year_key = 0
    # Reverse case_id for desc: invert each char's codepoint by negation
    # via tuple of negated ords. Cheap and total over UCD-FIL-### shape.
    id_key = tuple(-ord(c) for c in case_id)
    return (primary, ts_key, year_key, id_key)


def _build_repos_for_my_cases() -> Repos:
    """Per-render repo bundle. Mirrors ``_scope_from_request`` but exposed
    here so the My Cases render functions can be called from the timer
    without re-decoding the session — repos themselves are stateless."""
    return Repos(
        case=CsvCaseRepository(),
        segment=FilesystemRawSegmentRepository(),
        picklist=SqlitePicklistRepository(),
        pipeline_state=CsvPipelineStateRepository(),
        attention=SqliteAttentionItemsRepository(),
        case_manifest=CsvCaseManifestRepository(),
    )


# Brief #3.1 — one entry per case (or per attention-item slot context).
# The handler grouper below collapses an open attention_items query down
# to a per-case count for the expansion body.
def _attention_counts_by_case(items) -> dict[str, int]:
    """Group an iterable of :class:`AttentionItem` rows by ``case_id`` and
    return the count per case. Rows with no case_id (worker queue
    surfaces that don't carry one) are skipped because the surgeon's
    card view is per-case."""
    counts: dict[str, int] = defaultdict(int)
    for it in items:
        if it.case_id:
            counts[it.case_id] += 1
    return dict(counts)


# ----- card HTML helpers -----


def _format_card_header(
    case_id: str, case: dict, state: dict | None, badge: BadgeState
) -> str:
    """Single-line top row inside the card. Badge then case-id then a
    procedure / approach / date strip. ``case`` is the dict shape coming
    out of ``CsvCaseRepository.get_case``; ``state`` is the pipeline_state
    row dict (or ``None`` for queued cases)."""
    procedure = case.get("procedure_primary") or "—"
    approach = case.get("approach") or "—"
    date_str = _date_for_row(state, case) or "—"
    summary = f"{escape(procedure)} · {escape(approach)} · {escape(date_str)}"
    return (
        '<header class="ds-card-header">'
        f'{badge_html(badge)}'
        f'<span class="ds-card-case-id">{escape(case_id)}</span>'
        f'<span class="ds-card-type-label">{summary}</span>'
        '</header>'
    )


def _format_card_collapsed_body(case: dict) -> str:
    """One-liner indication row that always appears under the header,
    collapsed or expanded — keeps the card readable at a glance."""
    indication = case.get("indication") or "—"
    return (
        '<div class="ds-card-body">'
        f'<p class="ds-card-description">{escape(indication)}</p>'
        '</div>'
    )


def _format_expansion_body(
    case: dict,
    state: dict | None,
    badge: BadgeState,
    *,
    manifest: "CaseManifestRow | None",
    attention_count: int,
) -> str:
    """The expansion area — pipeline timeline, metadata strip, conditional
    additional/conversion lines, source segments list, related-attention
    link. Manifest values take precedence over the case-repo dict for the
    typed columns; the case dict is the fallback when no manifest row was
    found (shouldn't happen for owned cases, but defensive)."""
    parts: list[str] = ['<div class="ds-card-expansion">']

    # Pipeline timeline + last-update timestamp on the same logical line.
    last_update = _updated_for_row(state) or "—"
    parts.append(
        '<p class="ds-card-expansion-line">'
        '<span class="ds-card-expansion-label">Pipeline:</span></p>'
    )
    parts.append(pipeline_timeline_html(state, badge))
    parts.append(
        '<p class="ds-card-expansion-line">'
        f'<span class="ds-card-expansion-label">Last update:</span> '
        f'{escape(last_update)}'
        '</p>'
    )

    or_room = (
        (manifest.or_room if manifest else None)
        or case.get("or_room") or "—"
    )
    case_year = (
        (manifest.case_year if manifest else None)
        or case.get("case_year") or "—"
    )
    notes = (manifest.notes if manifest else "") or case.get("notes") or ""
    notes_display = escape(notes) if notes else "—"
    parts.append(
        '<p class="ds-card-expansion-line">'
        f'<span class="ds-card-expansion-label">OR:</span> {escape(str(or_room))} · '
        f'<span class="ds-card-expansion-label">Year:</span> {escape(str(case_year))} · '
        f'<span class="ds-card-expansion-label">Notes:</span> {notes_display}'
        '</p>'
    )

    additional = (
        list(manifest.procedure_additional) if manifest
        else list(case.get("procedure_additional") or [])
    )
    if additional:
        parts.append(
            '<p class="ds-card-expansion-line">'
            '<span class="ds-card-expansion-label">Additional procedure:</span> '
            f'{escape(", ".join(additional))}'
            '</p>'
        )

    conversion_target = (
        manifest.conversion_target if manifest
        else case.get("conversion_target") or ""
    )
    if conversion_target:
        parts.append(
            '<p class="ds-card-expansion-line">'
            '<span class="ds-card-expansion-label">Conversion:</span> '
            f'{escape(conversion_target)}'
            '</p>'
        )

    segments = list(state.get("raw_segments") or []) if state else []
    if segments:
        parts.append(
            '<p class="ds-card-expansion-line">'
            '<span class="ds-card-expansion-label">'
            f'Source segments ({len(segments)}):</span></p>'
        )
        parts.append('<ul>')
        for seg in segments:
            parts.append(f'<li>{escape(seg)}</li>')
        parts.append('</ul>')
    else:
        parts.append(
            '<p class="ds-card-expansion-line">'
            '<span class="ds-card-expansion-label">Source segments:</span> '
            '(none recorded)'
            '</p>'
        )

    if attention_count > 0:
        plural = "items" if attention_count != 1 else "item"
        parts.append(
            '<p class="ds-card-expansion-line">'
            '<span class="ds-card-expansion-label">'
            f'Related attention {plural}:</span> {attention_count} — see '
            'the Action Required tab.'
            '</p>'
        )

    parts.append('</div>')
    return "".join(parts)


def _my_case_card_html(
    case_id: str,
    case: dict,
    state: dict | None,
    badge: BadgeState,
    manifest: "CaseManifestRow | None",
    attention_count: int,
    *,
    is_expanded: bool,
) -> str:
    """Render one full card HTML — collapsed header + body, plus the
    expansion area when ``is_expanded``. Status stripe is bound to
    ``badge.value`` via the brand .ds-card-status-* family so the left
    edge color matches the in-card badge state."""
    status = badge.value
    classes = f"ds-card ds-card-expandable ds-card-status-{status}"
    parts = [
        f'<article class="{classes}" data-case-id="{escape(case_id)}" '
        f'data-status="{status}" data-expanded="{str(is_expanded).lower()}">',
        _format_card_header(case_id, case, state, badge),
        _format_card_collapsed_body(case),
    ]
    if is_expanded:
        parts.append(
            _format_expansion_body(
                case, state, badge,
                manifest=manifest,
                attention_count=attention_count,
            )
        )
    parts.append('</article>')
    return "".join(parts)


# ----- render fn -----
#
# Brief #3.1.4 — @gr.render dynamic card mount.
#
# Prior iterations (Brief #3.1 → #3.1.3) used a pre-allocated 50-slot
# pool of (gr.Group + gr.HTML + gr.Button) tuples with visibility
# toggling. Every render emitted 100 component updates, fanning out
# in Svelte 5's reactive flush and tripping ``effect_update_depth_
# exceeded`` even after per-slot states were removed, the .change
# bridge was dropped, and per-session memoization collapsed steady-
# state updates to ~1. The threshold-hitting fanout was structural to
# the pre-allocated pool — only mounting cards that actually exist
# eliminates the substrate entirely.
#
# Architecture after this refactor:
#
#   _load_my_cases_data(request) → (header, empty_update, footer,
#                                   visible_cases_payload)
#       Fetches the data, builds both collapsed_html + expanded_html
#       per visible case, and emits a 4-element tuple. No per-card
#       components in the output — just the payload.
#
#   @gr.render(inputs=[visible_cases_state, expanded_state])
#   def render_my_cases_cards(visible_cases, expanded):
#       For each entry in the payload, mounts gr.Group + gr.HTML +
#       gr.Button. Keys are stable on case_id (Gradio issues #11469 /
#       #12625 mitigation — same DOM identity across re-renders so
#       Svelte's reconciler doesn't tear and rebuild). Each button's
#       click handler is a fresh closure capturing the case_id.
#
#   blocks.load / timer.tick → _load_my_cases_data → writes the four
#       outputs. visible_cases_state.change auto-triggers @gr.render.
#
# No pre-allocated slots, no per-slot states, no .change bridges. The
# server-side topology stays as clean as Brief #3.1.3 left it, and the
# frontend Svelte flush only ever sees as many slot updates as there
# are actual cards to show.


def _empty_my_cases_payload(now: datetime) -> tuple:
    """Output for unauthenticated / no-cases renders: empty-state
    visible, no cards to mount."""
    return (
        "",                                                    # header_md
        gr.update(value=_EMPTY_CASES_MARKDOWN, visible=True),  # empty_state_md
        format_footer(now),                                    # footer_md
        [],                                                    # visible_cases_state
    )


def render_my_cases(
    expanded_case_id: str | None, request: gr.Request | None
) -> tuple:
    """Compute the My Cases data payload. Returns a 4-tuple that wires
    into ``[header_md, empty_state_md, footer_md, visible_cases_state]``:

        [0]  header_md value (counter strip string)
        [1]  empty_state_md update (visible toggle)
        [2]  footer_md value (auto-refresh timestamp)
        [3]  visible_cases_state list[dict] — each entry:
                {"case_id", "collapsed_html", "expanded_html"}

    The cards themselves are mounted by the ``@gr.render`` block in
    :func:`_build_my_cases`, which reads ``visible_cases_state`` plus
    ``expanded_state`` and dynamically mounts one Group/HTML/Button
    triple per visible case. This fn produces NO per-card components
    directly — that's the structural change vs Brief #3.1.3.

    ``expanded_case_id`` is accepted as an arg (kept signature-
    compatible with previous calls) but read only to validate that
    it's still in the visible window. The actual expansion is decided
    inside the @gr.render block per-render using the live state."""
    scope = _scope_from_request(request)
    now = _utcnow()
    if scope is None:
        return _empty_my_cases_payload(now)

    case_ids = scope.repos.case.list_owned_by(scope.folder_slug)
    if not case_ids:
        return _empty_my_cases_payload(now)

    states = scope.repos.pipeline_state.list_for_case_ids(case_ids)
    attention_flags = scope.repos.attention.has_attention_for_case_ids(case_ids)
    attention_items = scope.repos.attention.list_for_user(scope.username, "open")
    attention_counts = _attention_counts_by_case(attention_items)
    cases = {cid: scope.repos.case.get_case(cid) or {} for cid in case_ids}

    counts: dict[BadgeState, int] = defaultdict(int)
    ranked: list[tuple] = []
    for case_id in case_ids:
        case = cases[case_id]
        state = states.get(case_id)
        badge = derive_badge_state(
            state,
            attention_flags.get(case_id, False),
            now,
            _STUCK_THRESHOLD_MINUTES,
        )
        counts[badge] += 1
        ranked.append((case_id, case, state, badge))
    ranked.sort(key=lambda r: _sort_key(r[0], r[1], r[2]))
    truncated = ranked[:_MAX_VISIBLE_MY_CASES_SLOTS]

    visible_cases_payload: list[dict] = []
    for case_id, case, state, badge in truncated:
        manifest = scope.repos.case_manifest.for_case_id(case_id)
        attention_count = attention_counts.get(case_id, 0)
        collapsed_html = _my_case_card_html(
            case_id, case, state, badge,
            manifest=manifest,
            attention_count=attention_count,
            is_expanded=False,
        )
        expanded_html = _my_case_card_html(
            case_id, case, state, badge,
            manifest=manifest,
            attention_count=attention_count,
            is_expanded=True,
        )
        visible_cases_payload.append({
            "case_id": case_id,
            "collapsed_html": collapsed_html,
            "expanded_html": expanded_html,
        })

    return (
        format_counter_strip(counts),
        gr.update(visible=False),
        format_footer(now),
        visible_cases_payload,
    )


def _my_case_click_handler(
    clicked_case_id: str | None, expanded_case_id: str | None,
) -> "str | None":
    """Pure decision: toggle expansion for ``clicked_case_id`` against
    the current ``expanded_case_id``.

    Returns the new ``expanded_case_id`` state value:
    - ``str`` to expand / swap
    - ``None`` to collapse (clicked the already-expanded card)
    - ``None`` for invalid clicks (no case_id) — safe fallback

    Brief #3.1.4: signature simplified vs Brief #3.1.2's
    ``(slot_index, visible_cases, expanded_case_id)`` because the
    ``@gr.render`` block has the case_id in scope at mount time —
    each rendered button closure-captures it directly, no slot
    indirection."""
    if not clicked_case_id:
        return None
    if expanded_case_id == clicked_case_id:
        return None  # collapse
    return clicked_case_id  # expand (or swap)


def _build_my_cases(blocks: gr.Blocks) -> dict:
    """Construct the My Cases tab body. Cards mount dynamically via
    ``@gr.render`` (Brief #3.1.4) — no pre-allocated slot pool. The
    static surface is: header, empty-state Markdown, footer Markdown,
    and the two state seams (expanded_state, visible_cases_state).

    Returns a dict of components reachable from outside (tests +
    timer wiring at the build_surgeon_app level)."""
    header_md = gr.Markdown("", elem_id="my-cases-header")
    empty_state_md = gr.Markdown(
        _EMPTY_CASES_MARKDOWN, visible=True, elem_id="my-cases-empty",
    )
    # Two states at tab root — no per-slot states.
    expanded_state = gr.State(None)
    visible_cases_state = gr.State([])

    # Container for the dynamically-mounted cards. Kept as a static
    # gr.Group so the @gr.render block has a stable parent for layout +
    # CSS scoping; the cards themselves mount inside it.
    with gr.Group(elem_id="my-cases-cards") as cards_container:

        @gr.render(
            inputs=[visible_cases_state, expanded_state],
            # ``always_last`` (default) is what we want here — coalesce
            # bursts of state changes into a single render at the end.
        )
        def render_my_cases_cards(visible_cases, expanded):
            if not visible_cases:
                return
            for case_data in visible_cases:
                case_id = case_data["case_id"]
                is_expanded = (case_id == expanded)
                html = (
                    case_data["expanded_html"] if is_expanded
                    else case_data["collapsed_html"]
                )
                # Stable keys on case_id keep Svelte's reconciler from
                # tearing and rebuilding the DOM on re-render — the
                # Gradio issue #11469 / #12625 flicker + state-loss
                # mitigation. The key tuple includes a per-component
                # role so the Group, HTML, and Button each get their
                # own stable identity scoped to the same case_id.
                with gr.Group(key=("my-case-group", case_id)):
                    gr.HTML(html, key=("my-case-html", case_id))
                    btn_label = (
                        "Hide details" if is_expanded
                        else "View details"
                    )
                    btn = gr.Button(
                        btn_label,
                        variant="secondary",
                        size="sm",
                        key=("my-case-btn", case_id),
                    )

                    def _on_click(
                        current_expanded,
                        _case_id=case_id,
                    ):
                        return _my_case_click_handler(
                            _case_id, current_expanded,
                        )

                    btn.click(
                        _on_click,
                        inputs=[expanded_state],
                        outputs=[expanded_state],
                    )

    footer_md = gr.Markdown("", elem_id="my-cases-footer")
    timer = gr.Timer(value=30, active=True)

    # Output ordering — used by render_my_cases and the two
    # data-refresh triggers below. Only four entries: the static
    # markdowns plus the visible_cases payload. NO per-card
    # components in the output tuple.
    load_outputs: list = [
        header_md,            # [0]
        empty_state_md,       # [1]
        footer_md,            # [2]
        visible_cases_state,  # [3]
    ]

    blocks.load(
        render_my_cases,
        inputs=[expanded_state],
        outputs=load_outputs,
    )
    timer.tick(
        render_my_cases,
        inputs=[expanded_state],
        outputs=load_outputs,
    )
    # No explicit click-handler wiring at this scope — each per-card
    # button has its own click handler bound at @gr.render mount
    # time. visible_cases_state.change + expanded_state.change auto-
    # trigger the @gr.render block.

    return {
        "header_md": header_md,
        "empty_state_md": empty_state_md,
        "expanded_state": expanded_state,
        "visible_cases_state": visible_cases_state,
        "cards_container": cards_container,
        "footer_md": footer_md,
        "timer": timer,
    }


# ----- Action Required tab -----


# Pre-allocated card slot count. The Action Required tab uses a fixed
# pool of (Group + HTML + Button + States) tuples — Gradio can't create
# components dynamically on render, so we allocate a comfortable maximum
# at build time and toggle visibility per-render. 10 covers an active
# surgeon with several open items; overflow shows a "more items"
# overflow notice.
_MAX_VISIBLE_ACTION_CARDS = 10

_AR_EMPTY_HTML = (
    '<p class="ds-empty-state">No action items of concern.</p>'
)


def _start_of_day_utc_iso(now: datetime | None = None) -> str:
    """ISO 8601 timestamp for UTC midnight of ``now``'s day. v1
    approximation: surgeon-local timezone math is a follow-up. Used by
    ``count_actions_today`` to define the "today" cutoff."""
    n = now or datetime.now(timezone.utc)
    midnight = n.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat(timespec="seconds")


def _format_ar_timestamp(iso: str) -> str:
    """Card timestamp display: ``YYYY-MM-DD HH:MM UTC``. Falls back to
    the raw string if it's not parseable so a malformed row never takes
    the tab offline."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _action_card_html(item: AttentionItem) -> str:
    """One Action Required card. Returns the inner HTML (without the
    action button — the button is a real ``gr.Button`` widget alongside
    the HTML so click handlers can fire). Severity stripe + badge are
    bound to ``item.severity`` via the brand class system."""
    display = display_for_type(item.type)
    sev = item.severity if item.severity in ("normal", "high") else "normal"
    case_label = item.case_id if item.case_id else "—"
    parts = [
        f'<article class="ds-card ds-card-severity-{sev}" '
        f'data-item-id="{item.id}" data-item-type="{escape(item.type)}">',
        '<header class="ds-card-header">',
        f'<span class="ds-badge ds-badge-{sev}" data-severity="{sev}">'
        f'{sev.title()}</span>',
        f'<span class="ds-card-type-label">{escape(display.label)}</span>',
        f'<span class="ds-card-case-id">{escape(case_label)}</span>',
        f'<span class="ds-card-timestamp">'
        f'{escape(_format_ar_timestamp(item.created_at))}</span>',
        '</header>',
        '<div class="ds-card-body">',
        f'<p class="ds-card-description">{escape(display.description)}</p>',
    ]
    if item.details:
        parts.append(
            f'<p class="ds-card-details">{escape(item.details)}</p>'
        )
    parts.append('</div>')
    parts.append('</article>')
    return "".join(parts)


def _format_ar_counter(
    n_items: int, n_resolved_today: int, n_pending: int
) -> str:
    """Header counter strip — same visual shape as the My Cases strip.
    Format: ``"N items · M resolved today · K pending"``. Plurals
    intentionally consistent ("items" not "item")."""
    return (
        f"{n_items} items · {n_resolved_today} resolved today · "
        f"{n_pending} pending"
    )


def render_action_required(request: gr.Request | None) -> tuple:
    """Compute the Action Required tab data payload. Returns a 3-tuple
    that wires into ``[counter_md, empty_html, visible_attention_state]``:

        [0]  counter_md value (counter strip string)
        [1]  empty_html update (visible toggle)
        [2]  visible_attention_state list[dict] — each entry:
                {"item_id", "action", "html", "btn_label", "btn_visible"}

    The cards themselves are mounted by the ``@gr.render`` block in
    :func:`_build_action_required` (Brief #3.1.4) — no pre-allocated
    slot pool. This fn produces NO per-card components directly.

    Folder slug + username come exclusively from the session."""
    scope = _scope_from_request(request)
    if scope is None:
        return ("", gr.update(visible=True), [])

    items = scope.repos.attention.list_for_user(scope.username, "open")
    today_iso = _start_of_day_utc_iso()
    n_resolved_today = scope.repos.attention.count_actions_today(
        scope.username, today_iso
    )

    n_items = len(items)
    n_pending = n_items
    counter_text = _format_ar_counter(n_items, n_resolved_today, n_pending)

    if n_items == 0:
        return (counter_text, gr.update(visible=True), [])

    visible_items = items[:_MAX_VISIBLE_ACTION_CARDS]
    payload: list[dict] = []
    for it in visible_items:
        action = action_for_type(it.type)
        payload.append({
            "item_id": it.id,
            "action": action or "",
            "html": _action_card_html(it),
            "btn_label": action.title() if action else "",
            "btn_visible": action is not None,
        })

    return (counter_text, gr.update(visible=False), payload)


def _ar_action_handler(
    item_id: int, action: str, request: gr.Request | None,
) -> tuple:
    """Inner click handler: apply one (item_id, action) pair against the
    repo and return a fresh data payload for the Action Required tab.
    Validates via the repo (which raises on type-mismatch / scope-
    violation / already-closed). Kept as a distinct function so the
    (item_id, action) test surface stays unchanged.

    Race-graceful: ``AttentionItemAlreadyClosedError`` /
    ``AttentionItemNotFoundError`` / ``AttentionItemActionMismatchError``
    collapse to silent re-render — they indicate a stale tab or a
    double-click, both fixable by simply reloading the live state."""
    scope = _scope_from_request(request)
    if scope is None or not item_id:
        return render_action_required(request)
    try:
        if action == "dismiss":
            scope.repos.attention.dismiss(int(item_id), scope.username)
        elif action == "resolve":
            scope.repos.attention.resolve(int(item_id), scope.username)
        # Unknown action verbs (shouldn't happen — verb comes from our
        # own dispatch table) collapse to silent re-render.
    except (
        AttentionItemAlreadyClosedError, AttentionItemNotFoundError,
        AttentionItemActionMismatchError,
    ):
        # Race or UI-bypass — re-render to surface the live state.
        pass
    return render_action_required(request)


def _build_action_required(blocks: gr.Blocks) -> dict:
    """Construct the Action Required tab body. Cards mount dynamically
    via ``@gr.render`` (Brief #3.1.4) — no pre-allocated slot pool.
    Returns a dict of components reachable from outside (tests, timer
    wiring at the build_surgeon_app level)."""
    counter_md = gr.Markdown("", elem_id="ar-counter")
    empty_html = gr.HTML(_AR_EMPTY_HTML, visible=True, elem_id="ar-empty")
    visible_attention_state = gr.State([])

    with gr.Group(elem_id="ar-cards") as cards_container:

        @gr.render(inputs=[visible_attention_state])
        def render_ar_cards(visible_attention):
            if not visible_attention:
                return
            for entry in visible_attention:
                item_id = entry["item_id"]
                action = entry["action"]
                html = entry["html"]
                btn_visible = entry["btn_visible"]
                btn_label = entry["btn_label"]
                with gr.Group(key=("ar-group", item_id)):
                    gr.HTML(html, key=("ar-html", item_id))
                    if btn_visible:
                        with gr.Row():
                            gr.Markdown("")  # left spacer
                            btn = gr.Button(
                                btn_label, variant="primary",
                                key=("ar-btn", item_id),
                            )

                            def _on_click(
                                request: gr.Request,
                                _item_id=item_id,
                                _action=action,
                            ):
                                return _ar_action_handler(
                                    _item_id, _action, request,
                                )

                            btn.click(
                                _on_click,
                                inputs=None,
                                outputs=[
                                    counter_md, empty_html,
                                    visible_attention_state,
                                ],
                            )

    timer = gr.Timer(value=30, active=True)

    load_outputs: list = [counter_md, empty_html, visible_attention_state]
    blocks.load(render_action_required, None, load_outputs)
    timer.tick(render_action_required, None, load_outputs)

    return {
        "counter_md": counter_md,
        "empty_html": empty_html,
        "visible_attention_state": visible_attention_state,
        "cards_container": cards_container,
        "timer": timer,
    }


# ----- top-level Blocks build -----


# Surgeon-app CSS overlay. Two layers:
#
#   1. Brand state tokens + badge/timeline classes + expandable-card
#      family (MY_CASES_CSS) so the My Cases tab's pills, SVG timeline,
#      and card stripes render with brand colors.
#   2. Surgeon-app-specific overrides for the H2/H3 typography on tabs
#      and the "Signed in as" line, plus brand-colored tab indicator.
#
# Loaded via gr.Blocks(css=...) — that's the sanctioned path; injecting
# a <style> tag through a hidden gr.HTML can be swallowed when Gradio
# sets display:none on the component wrapper.
_SURGEON_APP_CSS = MY_CASES_CSS + """
/* ── Identity line + tab labels ── */
#surgeon-identity p,
#surgeon-identity {
  font-family: 'Fraunces', Georgia, serif !important;
  font-size: 22px !important;
  font-weight: 600 !important;
  color: var(--ds-primary) !important;
  letter-spacing: -0.01em;
  margin: 8px 0 12px 0;
}
.gradio-container button[role="tab"],
.gradio-container .tab-nav button {
  font-family: 'Fraunces', Georgia, serif !important;
  font-size: 18px !important;
  font-weight: 600 !important;
  letter-spacing: -0.01em;
  padding: 10px 18px !important;
  color: var(--ds-text) !important;
}

/* ── Tab indicator: brand teal, not Gradio's default orange ── */
.gradio-container button[role="tab"][aria-selected="true"],
.gradio-container .tab-nav button.selected {
  color: var(--ds-primary) !important;
  border-bottom-color: var(--ds-primary) !important;
  box-shadow: inset 0 -3px 0 var(--ds-primary) !important;
}
"""


# Brand teal in place of Gradio's default orange primary_hue. The brand
# CSS overrides backgrounds and text; this swaps the underlying token so
# anything we don't explicitly override (focus rings on inputs, link
# hover states, etc.) also picks up brand colors instead of orange.
#
# In Gradio 6 the theme= and css= kwargs moved from gr.Blocks() to
# launch() / mount_gradio_app(). We surface them as module attributes
# so app.main wires them through ``gr.mount_gradio_app(theme=..., css=...)``.
SURGEON_THEME = gr.themes.Default(
    primary_hue="teal",
    secondary_hue="teal",
    neutral_hue="slate",
)
SURGEON_CSS = _SURGEON_APP_CSS


def build_surgeon_app() -> gr.Blocks:
    with gr.Blocks(
        title="Surgeon — surgical-cv",
        analytics_enabled=False,
    ) as blocks:
        identity_md = gr.Markdown(elem_id="surgeon-identity")
        with gr.Tabs():
            with gr.Tab("Intake"):
                # Shared state across sections — Section 5 consumes all
                # of them at submit time.
                segments_state = gr.State([])
                selected_state = gr.State([])
                show_more_state = gr.State(False)
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

                # Section 5 updates this; lives above Section 1 per spec.
                success_banner_md = gr.Markdown("")

                _build_intake_section1(
                    blocks, segments_state, selected_state, show_more_state
                )
                _build_intake_section2(
                    blocks,
                    picklists_state,
                    procedure_primary_state,
                    procedure_additional_state,
                    approach_state,
                    conversion_target_state,
                )
                or_room_tb = _build_intake_section3(
                    blocks,
                    picklists_state,
                    case_year_state,
                    or_room_state,
                    indication_state,
                )
                notes_tb, notes_counter_md, notes_phi_warning_md = (
                    _build_intake_section4(
                        blocks, notes_state, notes_phi_warnings_state
                    )
                )
                _build_intake_section5(
                    blocks,
                    segments_state=segments_state,
                    selected_state=selected_state,
                    show_more_state=show_more_state,
                    picklists_state=picklists_state,
                    procedure_primary_state=procedure_primary_state,
                    procedure_additional_state=procedure_additional_state,
                    approach_state=approach_state,
                    conversion_target_state=conversion_target_state,
                    case_year_state=case_year_state,
                    or_room_state=or_room_state,
                    indication_state=indication_state,
                    notes_state=notes_state,
                    notes_phi_warnings_state=notes_phi_warnings_state,
                    or_room_tb=or_room_tb,
                    notes_tb=notes_tb,
                    notes_counter_md=notes_counter_md,
                    notes_phi_warning_md=notes_phi_warning_md,
                    success_banner_md=success_banner_md,
                )

                blocks.load(fetch_picklists, None, picklists_state)
            with gr.Tab("My Cases"):
                _build_my_cases(blocks)
            with gr.Tab("Action Required"):
                _build_action_required(blocks)
        blocks.load(_identity, None, identity_md)
    return blocks
