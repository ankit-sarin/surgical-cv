"""BDV recorder filename patterns — single source of truth.

The STERIS / BDV system writes raw segments as ``capt0_YYYYMMDD-HHMMSS.mp4``.
After the pipeline's Pass-1 concat claims a segment, it's renamed to
``capt0_YYYYMMDD-HHMMSS-copied.mp4``. Two consumers need to recognize these
filenames with different semantics:

  ``BDV_UNCLAIMED_RE`` — strict: only the canonical (unclaimed) form.
                         Used by ``app/repos/segments.py`` so the surgeon's
                         intake view excludes already-claimed segments.

  ``BDV_ANY_RE``       — accepts both canonical and ``-copied`` forms.
                         Used by ``pipeline/ffmpeg.py`` so callers reasoning
                         about already-moved segments can recover the
                         original timestamp.

F-015 split the patterns into this module so a future BDV recorder firmware
update only needs one file changed; the prior arrangement had the two regexes
in two different files with subtly different rules and no shared origin.
"""

from __future__ import annotations

import re

BDV_UNCLAIMED_RE: re.Pattern[str] = re.compile(
    r"^capt0_(\d{8})-(\d{6})\.mp4$"
)

BDV_ANY_RE: re.Pattern[str] = re.compile(
    r"^capt0_(\d{8}-\d{6})(?:-copied)?\.mp4$"
)
