"""Surgeon-side dispatch + display surface for attention_items types.

The Action Required tab needs three pieces of policy keyed on the
``attention_items.type`` string:

  1. Which action surfaces in the UI — ``"dismiss"`` (acknowledge an
     informational flag) or ``"resolve"`` (declare the issue handled).
  2. The human-readable label and one-liner description shown on the card.
  3. A defensive fallback when an unknown type appears — the worker may
     emit a new type ahead of UI mapping; we render the card read-only
     rather than crash the tab.

This module is the single source of truth so the repo's validation gate
(:class:`AttentionItemActionMismatchError`) and the Gradio handler agree
on which action belongs to which type. Adding a new type means one edit
here plus the worker emitter — no Gradio surgery required.

The ``"phi_redacted"`` slot is pre-wired for Brief #3.5 (PHI scanner
emitter); the dispatch table accepts the type today so the surgeon UI
will render it correctly the moment the emitter lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TypeDisplay:
    """Surgeon-facing label + description for one attention_items.type."""
    label: str
    description: str


# Action vocabulary surfaced on the card footer button. ``"dismiss"`` is
# acknowledgement (no underlying state to fix); ``"resolve"`` is a
# declaration that the issue was handled (re-submission, NAS cleanup,
# etc.). The repo validates that the chosen action matches this map so
# a UI bypass can't mark a hard failure as merely dismissed.
SURGEON_ACTION_BY_TYPE: dict[str, Literal["dismiss", "resolve"]] = {
    "verify_soft_fail": "dismiss",
    "pipeline_failure": "resolve",
    "orphan_marker": "resolve",
    "phi_redacted": "dismiss",  # pre-wired for Brief #3.5 emitter
}


SURGEON_TYPE_DISPLAY: dict[str, TypeDisplay] = {
    "verify_soft_fail": TypeDisplay(
        label="Quality flag",
        description=(
            "Automated review noted a concern; the case is complete "
            "and de-identified."
        ),
    ),
    "pipeline_failure": TypeDisplay(
        label="Processing failed",
        description=(
            "The case did not finish processing. Re-submission may "
            "be needed."
        ),
    ),
    "orphan_marker": TypeDisplay(
        label="Incomplete submission",
        description=(
            "Submission was received but case details are missing. "
            "Please re-enter via Intake."
        ),
    ),
    "phi_redacted": TypeDisplay(
        label="PHI redacted",
        description=(
            "Patient information was found and redacted from this "
            "case's text content."
        ),
    ),
}


def action_for_type(item_type: str) -> Literal["dismiss", "resolve"] | None:
    """Return the surgeon-side action for ``item_type`` or ``None`` for
    unknown types. ``None`` causes the card to render read-only — no
    action button — so an unmapped type doesn't take the tab offline."""
    return SURGEON_ACTION_BY_TYPE.get(item_type)


def display_for_type(item_type: str) -> TypeDisplay:
    """Return the surgeon-facing label + description for ``item_type``.
    Unknown types fall back to a generic title-cased label and a
    no-detail description so the card renders calmly even before its
    UI mapping lands."""
    mapped = SURGEON_TYPE_DISPLAY.get(item_type)
    if mapped is not None:
        return mapped
    # Title-case the raw type discriminator so e.g. ``foo_bar_baz``
    # becomes ``"Foo Bar Baz"`` — recognizable at a glance even without
    # a curated mapping.
    label = item_type.replace("_", " ").title() if item_type else "Notice"
    return TypeDisplay(
        label=label,
        description="No additional information available.",
    )
