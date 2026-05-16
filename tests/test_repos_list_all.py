"""Tests for the Brief #4 ``list_all`` / ``case_id_for_source_file``
extensions on CaseRepository, AttentionItemsRepository, and
PipelineStateRepository.

Coverage:

  - ``list_all`` on each repo returns the full set regardless of the
    caller's scope, mirroring the brief's contract: no role check
    inside the repo (auth lives at the admin mount).
  - ``case_id_for_source_file``: found / not-found / multi-claim
    (raises :class:`MultipleClaimsError`).
"""

from __future__ import annotations

import pytest

from app.exceptions import MultipleClaimsError
from app.repos import (
    AttentionItem,
    InMemoryAttentionItemsRepository,
    InMemoryCaseRepository,
    InMemoryPipelineStateRepository,
)


# ----- list_all -----


def test_case_repo_list_all_returns_every_row_unscoped():
    repo = InMemoryCaseRepository({
        "UCD-FIL-001": {"ucd_fil_id": "UCD-FIL-001", "surgeon": "sarin"},
        "UCD-FIL-002": {"ucd_fil_id": "UCD-FIL-002", "surgeon": "miller"},
        "UCD-FIL-003": {"ucd_fil_id": "UCD-FIL-003", "surgeon": "noren"},
    })
    rows = repo.list_all()
    assert {r["ucd_fil_id"] for r in rows} == {
        "UCD-FIL-001", "UCD-FIL-002", "UCD-FIL-003"
    }
    # Surgeon affiliation is preserved — the dashboard groups on it.
    assert {r["surgeon"] for r in rows} == {"sarin", "miller", "noren"}


def test_pipeline_state_repo_list_all_returns_every_row():
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {
            "ucd_fil_id": "UCD-FIL-001", "stage": "intake",
            "raw_segments": ["capt0_20260101-100000.mp4"],
            "intake_ts": "2026-01-01T10:00:00+00:00",
        },
        "UCD-FIL-002": {
            "ucd_fil_id": "UCD-FIL-002", "stage": "verified",
            "raw_segments": ["capt0_20260202-100000.mp4"],
            "verify_ts": "2026-02-02T11:00:00+00:00",
        },
    })
    rows = repo.list_all()
    assert {r["ucd_fil_id"] for r in rows} == {"UCD-FIL-001", "UCD-FIL-002"}


def test_attention_repo_list_all_returns_every_open_item_cross_silo():
    items = [
        AttentionItem(
            id=1, type="phi_redacted", case_id="UCD-FIL-001",
            affected_user="asarin", severity="normal",
            details="x", status="open",
            created_at="2026-05-15T08:00:00+00:00",
            created_by="system_worker",
            resolved_at=None, resolved_by=None, resolution_note=None,
            updated_at="2026-05-15T08:00:00+00:00",
        ),
        AttentionItem(
            id=2, type="pipeline_failure", case_id="UCD-FIL-002",
            affected_user="bmiller", severity="high",
            details="y", status="open",
            created_at="2026-05-15T08:01:00+00:00",
            created_by="system_worker",
            resolved_at=None, resolved_by=None, resolution_note=None,
            updated_at="2026-05-15T08:01:00+00:00",
        ),
        AttentionItem(  # closed — must not appear in default status='open'
            id=3, type="orphan_marker", case_id="UCD-FIL-003",
            affected_user="cnoren", severity="high",
            details="z", status="resolved",
            created_at="2026-05-15T08:02:00+00:00",
            created_by="system_worker",
            resolved_at="2026-05-15T09:00:00+00:00", resolved_by="cnoren",
            resolution_note=None,
            updated_at="2026-05-15T08:02:00+00:00",
        ),
    ]
    repo = InMemoryAttentionItemsRepository(items=items)
    rows = repo.list_all()
    # Cross-silo: spans multiple users. Filter is status='open' by
    # default — the resolved row doesn't appear.
    assert {r.id for r in rows} == {1, 2}
    assert {r.affected_user for r in rows} == {"asarin", "bmiller"}
    # Ordering: newest first by created_at, tiebreak id DESC.
    assert [r.id for r in rows] == [2, 1]


# ----- case_id_for_source_file -----


def test_case_id_for_source_file_found():
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {
            "ucd_fil_id": "UCD-FIL-001", "stage": "intake",
            "raw_segments": [
                "capt0_20260101-100000.mp4",
                "capt0_20260101-110000.mp4",
            ],
        },
        "UCD-FIL-002": {
            "ucd_fil_id": "UCD-FIL-002", "stage": "intake",
            "raw_segments": ["capt0_20260202-100000.mp4"],
        },
    })
    assert repo.case_id_for_source_file(
        "capt0_20260101-100000.mp4"
    ) == "UCD-FIL-001"
    assert repo.case_id_for_source_file(
        "capt0_20260101-110000.mp4"
    ) == "UCD-FIL-001"
    assert repo.case_id_for_source_file(
        "capt0_20260202-100000.mp4"
    ) == "UCD-FIL-002"


def test_case_id_for_source_file_not_found_returns_none():
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {
            "ucd_fil_id": "UCD-FIL-001", "stage": "intake",
            "raw_segments": ["capt0_20260101-100000.mp4"],
        },
    })
    assert repo.case_id_for_source_file(
        "capt0_99999999-999999.mp4"
    ) is None


def test_case_id_for_source_file_multiple_claims_raises():
    """Pipeline state corruption — should never happen given the intake
    invariant. The exception surfaces it to the admin queue rather than
    silently picking one claimant."""
    repo = InMemoryPipelineStateRepository({
        "UCD-FIL-001": {
            "ucd_fil_id": "UCD-FIL-001", "stage": "intake",
            "raw_segments": ["capt0_20260101-100000.mp4"],
        },
        "UCD-FIL-002": {
            "ucd_fil_id": "UCD-FIL-002", "stage": "intake",
            "raw_segments": ["capt0_20260101-100000.mp4"],
        },
    })
    with pytest.raises(MultipleClaimsError) as exc_info:
        repo.case_id_for_source_file("capt0_20260101-100000.mp4")
    err = exc_info.value
    assert err.source_file == "capt0_20260101-100000.mp4"
    assert set(err.case_ids) == {"UCD-FIL-001", "UCD-FIL-002"}
