"""Intake-time PHI pattern detector.

Used by the Intake form's Section 4 to surface a soft warning when the
surgeon's free-text notes appear to contain identifiers. The actual
post-submit PHI scrubbing runs later via gemma4:26b (Section 5 / pipeline-
side) and will reuse the same category vocabulary defined here, so this
module is the single source of truth for "what counts as a PHI category".

Category semantics (intentionally conservative — false positives are
acceptable since the intake-time warning is non-blocking):

  mrn:  unbroken runs of 7-10 digits. Also catches naked phone numbers;
        the ambiguity is intentional v1 behavior since both warrant a
        confirmation prompt.
  ssn:  XXX-XX-XXXX with dash or whitespace separators. Unseparated 9-digit
        runs fall through to mrn.
  date: M/D/YYYY (and D/M/YYYY, etc.) with ``/``, ``-``, or ``.`` separators.
        Year-only numbers (e.g. "2025") deliberately stay clean.

Returns counts only — call sites must not surface the matched text itself."""

from __future__ import annotations

import re

_MRN_PATTERN = re.compile(r"(?<!\d)\d{7,10}(?!\d)")
_SSN_PATTERN = re.compile(r"(?<!\d)\d{3}[-\s]\d{2}[-\s]\d{4}(?!\d)")
_DATE_PATTERN = re.compile(r"(?<!\d)\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}(?!\d)")


def scan_for_phi(text: str | None) -> dict[str, int]:
    """Return ``category → count`` for matched PHI patterns. Categories with
    zero matches are omitted, so an empty dict means clean text."""
    if not text:
        return {}
    counts: dict[str, int] = {}
    mrn_count = len(_MRN_PATTERN.findall(text))
    if mrn_count:
        counts["mrn"] = mrn_count
    ssn_count = len(_SSN_PATTERN.findall(text))
    if ssn_count:
        counts["ssn"] = ssn_count
    date_count = len(_DATE_PATTERN.findall(text))
    if date_count:
        counts["date"] = date_count
    return counts
