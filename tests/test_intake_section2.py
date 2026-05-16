"""Tests for the Intake tab's Section 2 (procedure + approach).

Pure-Python coverage: dropdown choice generation, duplicate detection,
fetch_picklists wired through the real SqlitePicklistRepository, scope
specialty propagation, and Blocks construction shape. The dynamically
rendered dropdowns / radios live inside ``@gr.render`` and require a
running Gradio runtime to exercise — covered by the uvicorn smoke."""

from __future__ import annotations

from types import SimpleNamespace

import gradio as gr
import pytest

from app.auth import SESSION_COOKIE_NAME, encode_session
from app.repos.picklists import PicklistValue
from app.scopes import AdminScope, SurgeonScope
from app.surgeon_app import (
    _CONV_PENDING,
    _find_duplicates,
    _picklist_choices,
    _scope_from_request,
    build_surgeon_app,
    fetch_picklists,
)


def _make_request(token: str | None):
    cookies = {SESSION_COOKIE_NAME: token} if token else {}
    return SimpleNamespace(cookies=cookies, headers={})


# ----- _picklist_choices: (display_label, value) tuples for Gradio -----


def test_picklist_choices_preserves_order():
    values = [
        PicklistValue("A", "A", 10),
        PicklistValue("B", "B", 20),
        PicklistValue("C", "C", 30),
    ]
    assert _picklist_choices(values) == [("A", "A"), ("B", "B"), ("C", "C")]


def test_picklist_choices_uses_display_label_first():
    values = [PicklistValue(value="laparoscopic", display_label="Laparoscopic", sort_order=10)]
    assert _picklist_choices(values) == [("Laparoscopic", "laparoscopic")]


def test_picklist_choices_empty_input():
    assert _picklist_choices([]) == []


# ----- _find_duplicates: primary + additionals -----


def test_find_duplicates_none():
    assert _find_duplicates("Sigmoidectomy", ["Other"]) == []


def test_find_duplicates_additional_in_primary():
    assert _find_duplicates("Sigmoidectomy", ["Sigmoidectomy"]) == ["Sigmoidectomy"]


def test_find_duplicates_two_additionals_match():
    assert _find_duplicates(
        "Right hemicolectomy", ["Sigmoidectomy", "Sigmoidectomy"]
    ) == ["Sigmoidectomy"]


def test_find_duplicates_ignores_none_slots():
    """Newly-added empty slots (value=None) must not appear as duplicates
    even when there are several of them."""
    assert _find_duplicates("Sigmoidectomy", [None, None, None]) == []


def test_find_duplicates_ignores_empty_string_slots():
    assert _find_duplicates("Sigmoidectomy", ["", "Sigmoidectomy"]) == ["Sigmoidectomy"]


def test_find_duplicates_no_primary():
    assert _find_duplicates(None, ["Sigmoidectomy", "Sigmoidectomy"]) == ["Sigmoidectomy"]


def test_find_duplicates_multiple_distinct_duplicates():
    out = _find_duplicates("A", ["A", "B", "B"])
    assert sorted(out) == ["A", "B"]


# ----- fetch_picklists: real SqlitePicklistRepository under the hood -----


def test_fetch_picklists_returns_seeded_procedures(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    assert {p.value for p in data["procedure"]} == {
        "Right hemicolectomy", "Sigmoidectomy", "Low anterior resection", "Other",
    }


def test_fetch_picklists_returns_universal_approaches(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    assert {p.value for p in data["approach"]} == {
        "Open", "Laparoscopic", "Robotic", "Hybrid",
    }


def test_fetch_picklists_procedures_sorted(app_env):
    req = _make_request(token=encode_session("asarin"))
    data = fetch_picklists(req)
    sort_orders = [p.sort_order for p in data["procedure"]]
    assert sort_orders == sorted(sort_orders)


def test_fetch_picklists_unauthenticated_returns_empty(app_env):
    """Defense-in-depth: the gradio mount's auth_dep already gated /app/,
    but fetch_picklists must degrade gracefully across every field."""
    data = fetch_picklists(_make_request(token=None))
    assert set(data.keys()) == {"procedure", "approach", "case_year", "indication"}
    assert all(v == [] for v in data.values())


def test_fetch_picklists_admin_returns_empty(app_env):
    """Intake is surgeon-only — admin requests get the empty shape."""
    req = _make_request(token=encode_session("ankitsarin"))
    data = fetch_picklists(req)
    assert all(v == [] for v in data.values())


# ----- scope specialty propagation -----


def test_surgeon_scope_carries_specialty(app_env):
    req = _make_request(token=encode_session("asarin"))
    scope = _scope_from_request(req)
    assert scope is not None
    assert scope.specialty == "colorectal"


def test_admin_scope_specialty_is_none():
    from app.repos import (
        InMemoryAttentionItemsRepository,
        InMemoryCaseManifestRepository,
        InMemoryCaseRepository,
        InMemoryPicklistRepository,
        InMemoryPipelineStateRepository,
        InMemoryRawSegmentRepository,
        Repos,
    )

    repos = Repos(
        case=InMemoryCaseRepository(),
        segment=InMemoryRawSegmentRepository(),
        picklist=InMemoryPicklistRepository(),
        pipeline_state=InMemoryPipelineStateRepository(),
        attention=InMemoryAttentionItemsRepository(),
        case_manifest=InMemoryCaseManifestRepository(),
    )
    scope = AdminScope("ankitsarin", repos)
    assert scope.specialty is None


def test_surgeon_scope_specialty_default_none():
    """If not explicitly passed, specialty defaults to None (covers the
    test-fixture path where the scope is built without a user record)."""
    from app.repos import (
        InMemoryAttentionItemsRepository,
        InMemoryCaseManifestRepository,
        InMemoryCaseRepository,
        InMemoryPicklistRepository,
        InMemoryPipelineStateRepository,
        InMemoryRawSegmentRepository,
        Repos,
    )

    repos = Repos(
        case=InMemoryCaseRepository(),
        segment=InMemoryRawSegmentRepository(),
        picklist=InMemoryPicklistRepository(),
        pipeline_state=InMemoryPipelineStateRepository(),
        attention=InMemoryAttentionItemsRepository(),
        case_manifest=InMemoryCaseManifestRepository(),
    )
    scope = SurgeonScope("asarin", "sarin", repos)
    assert scope.specialty is None


# ----- picklist access uses scope.repos directly (per Spec G) -----


def test_picklist_access_via_scope_repos(app_env):
    """Section 2's documented access pattern: scope.repos.picklist.list_active
    rather than a scope method. Verify the pattern works."""
    req = _make_request(token=encode_session("asarin"))
    scope = _scope_from_request(req)
    assert scope is not None
    procs = scope.repos.picklist.list_active("procedure", scope.specialty)
    assert len(procs) >= 1
    apprs = scope.repos.picklist.list_active("approach", scope.specialty)
    assert len(apprs) == 4


# ----- conversion sentinel -----


def test_conversion_sentinel_is_empty_string():
    """_CONV_PENDING distinguishes 'checkbox checked, no target yet' from
    'unchecked' (None) and 'checked with target' (a real approach value)."""
    assert _CONV_PENDING == ""
    # And it's not equal to None.
    assert _CONV_PENDING is not None


# ----- Blocks construction shape -----


def test_intake_tab_carries_seven_state_components():
    """Spec G adds 4 new gr.State (procedure_primary, procedure_additional,
    approach, conversion_target) on top of Section 1's 3 (segments, selected,
    show_more) → 7 total inside the Intake tab. Plus picklists_state for
    dropdown choices = 7 effective state seams."""
    blocks = build_surgeon_app()
    state_count = sum(
        1 for c in blocks.blocks.values() if isinstance(c, gr.State)
    )
    # 3 from Section 1 (segments, selected, show_more) + picklists_state
    # + 4 from Section 2 (procedure_primary, procedure_additional,
    # approach, conversion_target) = 8 total.
    assert state_count >= 8


def test_intake_tab_carries_section2_header():
    """Static gr.Markdown for the Section 2 header is in the Blocks tree."""
    blocks = build_surgeon_app()
    markdown_values = [
        c.value for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    # The Section 2 header markdown is one of the static markdowns.
    assert any(
        v and "Section 2" in str(v) for v in markdown_values
    )


def test_intake_tab_carries_section1_header():
    """Section 1's static header survives the Spec G refactor."""
    blocks = build_surgeon_app()
    markdown_values = [
        c.value for c in blocks.blocks.values() if isinstance(c, gr.Markdown)
    ]
    assert any(v and "Section 1" in str(v) for v in markdown_values)
