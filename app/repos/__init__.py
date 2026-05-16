"""Repository package: data-access layer for case ownership, raw segments,
and picklists.

``Repos`` is the per-request bundle scopes hold; future repos (pipeline
state, attention items, etc.) land here as their respective tabs are
specced.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.repos.attention import (  # noqa: F401  — re-export
    AttentionItem,
    AttentionItemAlreadyClosedError,
    AttentionItemActionMismatchError,
    AttentionItemNotFoundError,
    AttentionItemNotResolvableError,
    AttentionItemsRepository,
    AttentionRepoError,
    InMemoryAttentionItemsRepository,
    SqliteAttentionItemsRepository,
)
from app.repos.case_manifest import (  # noqa: F401  — re-export
    CaseManifestRepository,
    CaseManifestRow,
    CsvCaseManifestRepository,
    InMemoryCaseManifestRepository,
)
from app.repos.cases import (  # noqa: F401  — re-export
    CaseRepository,
    CsvCaseRepository,
    InMemoryCaseRepository,
    RepoIntegrityError,
    SubmitError,
    SubmitResult,
)
from app.repos.picklists import (  # noqa: F401  — re-export
    InMemoryPicklistRepository,
    PicklistRepository,
    PicklistValue,
    SqlitePicklistRepository,
)
from app.repos.pipeline_state import (  # noqa: F401  — re-export
    CsvPipelineStateRepository,
    InMemoryPipelineStateRepository,
    PipelineStateRepository,
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
    picklist: PicklistRepository
    pipeline_state: PipelineStateRepository
    attention: AttentionItemsRepository
    case_manifest: CaseManifestRepository
