"""Intake-time PHI pattern detector.

Used by the Intake form's Section 4 to surface a soft warning when the
surgeon's free-text notes appear to contain identifiers. The post-submit
PHI scrubber referenced in v18 (planned: gemma4:26b) will reuse the same
category vocabulary defined here.

Patterns live in ``pipeline/phi_patterns.py`` so both this module
(intake-time soft warning) and ``pipeline/phi_redact.py`` (persistence-
time scrubbing) share a single source of truth. ``pipeline/`` is the
deeper layer; ``app/`` may import from it freely.

Category semantics (intentionally permissive — false positives are
acceptable since the intake-time warning is non-blocking; false negatives
on names are NOT — bias the name heuristic toward catching more):

  mrn:      unbroken runs of 7-10 digits. Catches naked phone numbers too.
  ssn:      XXX-XX-XXXX with dash or whitespace separators.
  date:     M/D/YYYY (with /, -, or .). Year-only stays clean.
  name:     consecutive title-case tokens after Dr. / Pt. / Patient: /
            MRN: / Mr. / Mrs. / Ms. prefixes.
  phone:    separator-formatted 10-digit phones — distinct from naked mrn.
  address:  number + title-case word(s) + street suffix. Minimal.

Returns counts only — call sites must not surface the matched text itself.
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


# Stable category-name → compiled-pattern map. Iteration order is preserved
# (Python ≥3.7) so call sites can rely on a deterministic count layout.
# Order chosen to match user-mental-priority: MRN/SSN (most clearly PHI),
# then date, then name, then phone (the new categories).
_CATEGORIES: tuple[tuple[str, "object"], ...] = (
    ("mrn", MRN_PATTERN),
    ("ssn", SSN_PATTERN),
    ("date", DATE_PATTERN),
    ("name", NAME_PATTERN),
    ("phone", PHONE_PATTERN),
    ("address", ADDRESS_PATTERN),
)


def scan_for_phi(text: str | None) -> dict[str, int]:
    """Return ``category → count`` for matched PHI patterns. Categories with
    zero matches are omitted, so an empty dict means clean text."""
    if not text:
        return {}
    counts: dict[str, int] = {}
    for name, pattern in _CATEGORIES:
        n = len(pattern.findall(text))
        if n:
            counts[name] = n
    return counts


def format_phi_details(categories) -> str:
    """Brief #3.5b: render a PHI-categories set into the canonical
    rollup-attention-item ``details`` string.

    Accepts any iterable / mapping whose membership test (``c in
    categories``) returns True for category names. Most natural inputs
    are :func:`scan_for_phi`'s dict, a ``frozenset``/``set``, a list,
    or a tuple — all four work.

    Returns the empty string for an empty / falsy input. Callers
    treat the empty string as the "do not emit" signal — the worker
    skips the upsert when ``format_phi_details(scan_result) == ""``.

    Output for non-empty input:

        ``PHI was found and redacted. Categories: <c1>, <c2>, <c3>.``

    Categories appear in :data:`_CATEGORIES` order (mrn, ssn, date,
    name, phone, address), filtered to those present in ``categories``,
    lowercased (no transform — phi.py's vocabulary is already lower).
    The string is intentionally safe for direct rendering: it names
    categories only, never values."""
    if not categories:
        return ""
    present = [name for name, _ in _CATEGORIES if name in categories]
    if not present:
        # Defensive: the input had items but none matched the canonical
        # vocabulary. Treat as empty so we never emit a malformed line.
        return ""
    return f"PHI was found and redacted. Categories: {', '.join(present)}."
