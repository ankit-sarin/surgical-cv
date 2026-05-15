"""Redaction primitives for operator-visible surfaces.

Two distinct shapes:

  ``redact_field(field_name, value)`` — *field-aware* redaction. Used at
  presentation time (``metadata --show``) and anywhere a manifest column
  is rendered without context. Known sensitive fields collapse to
  ``<redacted, length=N>``; everything else passes through. The length
  hint is intentional — operators can confirm a value is non-empty
  without seeing its content.

  ``scrub_text(text)`` — *content-aware* redaction. Used at persistence
  time (worker stderr → ``attention_items.details``) where the field
  identity is unknown but the text may contain identifiers. Replaces
  matched patterns with category placeholders so the surrounding context
  (filename, error class, line number) survives.

Empty / None inputs preserve the prior UX in both surfaces:
``redact_field("notes", "")`` returns ``"(empty)"``; ``scrub_text("")``
returns ``""``.
"""

from __future__ import annotations

from pipeline.phi_patterns import (
    ADDRESS_PATTERN,
    DATE_PATTERN,
    MRN_PATTERN,
    NAME_PATTERN,
    PHONE_PATTERN,
    SSN_PATTERN,
)

# Field names whose values must never appear in operator-visible output.
# Extendable — add columns here as the manifest grows. The metadata CLI's
# show path is the canonical consumer.
REDACTED_FIELDS: frozenset[str] = frozenset({"notes"})

# Replacement order matters: more specific patterns (SSN, PHONE) run before
# the broader MRN pattern so a phone like ``916-555-1234`` doesn't get
# partially eaten by MRN's 7-10 digit match against the trailing ``5551234``.
# (PHONE has its own separator-aware bounds, but ordering keeps the intent
# explicit.) Address before name so "123 Main St" isn't decomposed into
# pieces that name might partially claim.
_SCRUB_PASSES: tuple[tuple, ...] = (
    (SSN_PATTERN, "<SSN>"),
    (PHONE_PATTERN, "<PHONE>"),
    (DATE_PATTERN, "<DATE>"),
    (ADDRESS_PATTERN, "<ADDRESS>"),
    (NAME_PATTERN, "<NAME>"),
    (MRN_PATTERN, "<MRN>"),
)


def redact_field(field_name: str, value: str) -> str:
    """Field-aware redaction.

    For fields in ``REDACTED_FIELDS``: returns ``<redacted, length=N>`` for
    non-empty values, ``(empty)`` for empty / None values. The length hint
    is the original character count — useful for "did the surgeon enter
    anything?" triage without exposing the content.

    For all other fields: returns ``value`` unchanged (or ``""`` if None).
    """
    if field_name in REDACTED_FIELDS:
        if not value:
            return "(empty)"
        return f"<redacted, length={len(value)}>"
    return value if value is not None else ""


def scrub_text(text: str) -> str:
    """Content-aware redaction.

    Walks the input through every pattern in ``_SCRUB_PASSES`` and replaces
    each match with the corresponding placeholder. Non-PHI text passes
    through verbatim. Empty / None input returns ``""``.

    Order is intentional (see ``_SCRUB_PASSES`` comment): more specific
    patterns first so they consume their text before broader patterns can
    partially match it.
    """
    if not text:
        return ""
    out = text
    for pattern, placeholder in _SCRUB_PASSES:
        out = pattern.sub(placeholder, out)
    return out
