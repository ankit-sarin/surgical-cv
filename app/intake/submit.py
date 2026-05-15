"""Intake submission entry point.

``validate_submission`` is the hard-gate accumulator: walks every state seam
the surgeon UI carries (Sections 1-4) and returns the full error list rather
than failing on the first miss. Section 5 renders the list as a single block
at the top of the form — surgeons fix everything at once, no progressive
discovery via field-level popups.

``handle_submit_request`` is the Gradio-facing orchestrator: branches on
validation-fail / PHI-confirm-required / clean-to-submit, defers the actual
write to ``CaseRepository.submit_case``.

The PHI-confirm gate doesn't re-run the regex scan here — Section 4 already
populated ``notes_phi_warnings_state`` at every notes-textbox blur, and that
state is what we read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.repos import PicklistValue
from app.repos.cases import SubmitError, SubmitResult


@dataclass(frozen=True)
class ValidationContext:
    """Bundle of state values the validator walks. Mirrors the gr.State
    seams in surgeon_app.py rather than the on-disk manifest schema —
    e.g., ``conversion_target = ""`` is the Spec G "checked but no target"
    sentinel, distinct from ``None`` ("not a conversion case")."""

    segments_selected: list[str]
    procedure_primary: str | None
    procedure_additional: list[str]
    approach: str | None
    conversion_target: str | None
    case_year: str | None
    or_room: str | None
    indication: str | None


def _vocab(picklists: dict[str, list[PicklistValue]], field: str) -> set[str]:
    return {v.value for v in picklists.get(field, [])}


def validate_submission(
    ctx: ValidationContext,
    picklists: dict[str, list[PicklistValue]],
) -> list[str]:
    """Return human-readable error strings, in section order. Empty list =
    clean — submit may proceed (subject to the PHI confirm gate)."""
    errors: list[str] = []

    if not ctx.segments_selected:
        errors.append("Select at least one segment from Section 1.")

    proc_vocab = _vocab(picklists, "procedure")
    if not ctx.procedure_primary:
        errors.append("Primary procedure required.")
    elif ctx.procedure_primary not in proc_vocab:
        errors.append(
            f"Primary procedure {ctx.procedure_primary!r} is not in the "
            "vocabulary."
        )

    for item in ctx.procedure_additional:
        if item not in proc_vocab:
            errors.append(
                f"Additional procedure {item!r} is not in the vocabulary."
            )
    if ctx.procedure_primary and ctx.procedure_primary in ctx.procedure_additional:
        errors.append(
            f"Additional procedures contain the primary "
            f"{ctx.procedure_primary!r}; each procedure may appear once."
        )
    if len(ctx.procedure_additional) != len(set(ctx.procedure_additional)):
        errors.append("Additional procedures contain a duplicate.")

    appr_vocab = _vocab(picklists, "approach")
    if not ctx.approach:
        errors.append("Approach required.")
    elif ctx.approach not in appr_vocab:
        errors.append(
            f"Approach {ctx.approach!r} is not in the vocabulary."
        )

    if ctx.conversion_target is not None:
        if ctx.conversion_target == "":
            errors.append(
                "Conversion is checked — select a conversion target or "
                "uncheck Converted."
            )
        elif ctx.conversion_target not in appr_vocab:
            errors.append(
                f"Conversion target {ctx.conversion_target!r} is not in "
                "the vocabulary."
            )
        elif ctx.approach and ctx.conversion_target == ctx.approach:
            errors.append(
                "Conversion target cannot equal the primary approach."
            )

    year_vocab = _vocab(picklists, "case_year")
    if not ctx.case_year:
        errors.append("Case year required.")
    elif ctx.case_year not in year_vocab:
        errors.append(
            f"Case year {ctx.case_year!r} is not in the allowlist."
        )

    if not ctx.or_room:
        errors.append("OR room required.")

    ind_vocab = _vocab(picklists, "indication")
    if not ctx.indication:
        errors.append("Indication required.")
    elif ctx.indication not in ind_vocab:
        errors.append(
            f"Indication {ctx.indication!r} is not in the vocabulary."
        )

    return errors


def format_validation_errors(errors: list[str]) -> str:
    """Render the error list as a single Markdown block for Section 5's
    error surface. Empty list → empty string."""
    if not errors:
        return ""
    bullets = "\n".join(f"- {e}" for e in errors)
    return f"⚠️ **Please fix the following before submitting:**\n\n{bullets}"


def build_partial_row(
    surgeon: str, ctx: ValidationContext, notes: str | None
) -> dict:
    """Project the validated context into the dict shape
    ``CaseRepository.submit_case`` expects. ``conversion_target`` is
    normalized from None / "" / value into the on-disk string convention."""
    return {
        "surgeon": surgeon,
        "case_year": ctx.case_year,
        "or_room": ctx.or_room,
        "procedure_primary": ctx.procedure_primary,
        "procedure_additional": list(ctx.procedure_additional),
        "approach": ctx.approach,
        # gr.State carries None (no conversion) or a non-empty string
        # (chosen target) after validation. The "" sentinel was rejected
        # at validation time.
        "conversion_target": ctx.conversion_target or "",
        "indication": ctx.indication,
        "notes": notes or "",
    }


@dataclass(frozen=True)
class SubmitOutcome:
    """Result of ``handle_submit_request``. Gradio adapter converts to the
    component-update tuple."""

    kind: Literal["validation_error", "phi_confirm", "success", "infra_error"]
    error_block: str = ""
    submit_result: SubmitResult | None = None
    infra_error: str = ""


def handle_submit_request(
    surgeon: str,
    ctx: ValidationContext,
    notes: str | None,
    notes_phi_warnings: dict[str, int] | None,
    picklists: dict[str, list[PicklistValue]],
    segment_filenames: list[str],
    *,
    submit_fn,
    phi_already_confirmed: bool,
) -> SubmitOutcome:
    """Pure orchestration.

    ``submit_fn`` is the repo's ``submit_case`` bound method — injected so
    tests can swap an in-memory implementation in without touching the
    filesystem.

    ``phi_already_confirmed`` is True when called from the Confirm button
    (PHI dialog already presented and accepted). In that path the PHI check
    is bypassed even if warnings exist.
    """
    errors = validate_submission(ctx, picklists)
    if errors:
        return SubmitOutcome(
            kind="validation_error",
            error_block=format_validation_errors(errors),
        )

    if notes_phi_warnings and not phi_already_confirmed:
        return SubmitOutcome(kind="phi_confirm")

    partial_row = build_partial_row(surgeon, ctx, notes)
    try:
        result = submit_fn(partial_row, segment_filenames)
    except SubmitError as e:
        return SubmitOutcome(kind="infra_error", infra_error=str(e))
    return SubmitOutcome(kind="success", submit_result=result)
