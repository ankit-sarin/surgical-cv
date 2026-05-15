"""PHI regex patterns — single source of truth for the surgical-cv codebase.

Both ``app/phi.py`` (intake-time soft warning) and ``pipeline/phi_redact.py``
(persistence-time scrubbing of operator-visible surfaces) import from here.
The split exists so the patterns can be reused without ``pipeline/`` having
to import from ``app/`` (layering rule: ``pipeline/`` is the deeper layer).

Category semantics (intentionally permissive — false positives are tolerable
for non-blocking warnings and over-redaction at persistence time, but false
negatives on names are NOT — bias the name heuristic toward catching more):

  mrn:      unbroken runs of 7-10 digits. Also catches naked phone numbers;
            the ambiguity is intentional v1 behavior.
  ssn:      XXX-XX-XXXX with dash or whitespace separators. Unseparated
            9-digit runs fall through to mrn.
  date:     M/D/YYYY (and D/M/YYYY, etc.) with ``/``, ``-``, or ``.``
            separators. Year-only numbers (e.g. "2025") deliberately stay
            clean.
  name:     consecutive title-case tokens (capitalized first letter,
            lowercase rest) preceded by a clinical-context prefix:
            ``Dr.`` / ``Dr `` / ``Pt.`` / ``Pt `` / ``Patient:`` /
            ``MRN:`` / ``Mr.`` / ``Mrs.`` / ``Ms.``. The prefix is
            consumed by the lookbehind / non-capturing group; the match
            covers only the name token(s). Prefix list intentionally
            requires either punctuation or a deliberate clinical prefix
            so generic capitalized words ("Patient", "Surgery") don't
            misfire on their own.
  phone:    separator-formatted 10-digit phone numbers — ``(NPA) NXX-XXXX``,
            ``NPA-NXX-XXXX``, ``NPA.NXX.XXXX``, ``NPA NXX XXXX``. Naked
            10-digit runs are caught by mrn instead.
  address:  number + one-or-more title-case word(s) + street suffix
            (St / Ave / Rd / Blvd / Dr / Ln / Way / Pl / Ct), optional
            trailing period. Minimal, false-positive tolerable. The leading
            number disambiguates "Acacia Dr." (a street) from "Dr. Smith"
            (a doctor prefix).
"""

from __future__ import annotations

import re

# ----- existing patterns (moved verbatim from app/phi.py) -----

MRN_PATTERN = re.compile(r"(?<!\d)\d{7,10}(?!\d)")
SSN_PATTERN = re.compile(r"(?<!\d)\d{3}[-\s]\d{2}[-\s]\d{4}(?!\d)")
DATE_PATTERN = re.compile(r"(?<!\d)\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}(?!\d)")

# ----- new patterns -----

# Name prefixes: punctuation-anchored or whitespace-anchored variants. The
# prefix is part of the match (we replace the whole region during scrub) but
# only the name tokens after the prefix carry PHI value.
_NAME_PREFIX = (
    r"(?:Dr\.\s+|Dr\s+|Pt\.\s+|Pt\s+|Patient:\s*|MRN:\s*"
    r"|Mr\.\s+|Mrs\.\s+|Ms\.\s+)"
)
_NAME_TOKEN = r"[A-Z][a-z]+"
NAME_PATTERN = re.compile(
    rf"\b{_NAME_PREFIX}{_NAME_TOKEN}(?:\s+{_NAME_TOKEN})*"
)

# Phone: 10-digit with explicit separator. The ``(?<!\d)`` / ``(?!\d)`` bounds
# prevent matching inside a longer digit run (which mrn handles).
PHONE_PATTERN = re.compile(
    r"(?<!\d)\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"
)

# Address: leading number, one or more title-case words, common street suffix.
# The trailing ``\b`` keeps the suffix from spilling into adjacent text.
_STREET_SUFFIX = r"(?:St|Ave|Rd|Blvd|Dr|Ln|Way|Pl|Ct)"
ADDRESS_PATTERN = re.compile(
    rf"\b\d+\s+(?:[A-Z][a-z]+\s+)+{_STREET_SUFFIX}\b\.?"
)
