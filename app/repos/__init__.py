"""Repository package: data-access layer for case ownership and raw segments.

``Repos`` is the per-request bundle that scopes hold; future repos for
pipeline state, attention items, etc. land here as their respective tabs
are specced.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.repos.cases import (  # noqa: F401  — re-export
    CaseRepository,
    CsvCaseRepository,
    InMemoryCaseRepository,
)
from app.repos.segments import (  # noqa: F401  — re-export
    FilesystemRawSegmentRepository,
    InMemoryRawSegmentRepository,
    RawSegmentRepository,
    SegmentRecord,
)


@dataclass(frozen=True)
class Repos:
    case: CaseRepository
    segment: RawSegmentRepository
