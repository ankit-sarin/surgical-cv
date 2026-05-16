"""Tests for ``app/attention_actions.py`` — the dispatch + display
surface that maps attention_items.type strings to surgeon-side actions
and human-readable labels."""

from __future__ import annotations

import pytest

from app.attention_actions import (
    SURGEON_ACTION_BY_TYPE,
    SURGEON_TYPE_DISPLAY,
    TypeDisplay,
    action_for_type,
    display_for_type,
)


# ----- action_for_type -----


@pytest.mark.parametrize("item_type,expected", [
    ("verify_soft_fail", "dismiss"),
    ("pipeline_failure", "resolve"),
    ("orphan_marker", "resolve"),
    ("phi_redacted", "dismiss"),
])
def test_action_for_type_returns_documented_value(item_type, expected):
    assert action_for_type(item_type) == expected


def test_action_for_type_unknown_returns_none():
    """Unknown types render read-only — no action button. The brief
    expects None so the UI can detect the unmapped case."""
    assert action_for_type("totally_unknown_type") is None
    assert action_for_type("") is None


def test_dispatch_table_only_emits_dismiss_or_resolve():
    """Defense in depth — every entry in the table must be one of the
    two recognized verbs so a typo doesn't silently break the UI."""
    for verb in SURGEON_ACTION_BY_TYPE.values():
        assert verb in ("dismiss", "resolve")


def test_dispatch_table_covers_brief_three_known_worker_types():
    """Worker emits these three today; phi_redacted is pre-wired for
    Brief #3.5."""
    for required in ("verify_soft_fail", "pipeline_failure", "orphan_marker"):
        assert required in SURGEON_ACTION_BY_TYPE


# ----- display_for_type -----


@pytest.mark.parametrize("item_type,expected_label", [
    ("verify_soft_fail", "Quality flag"),
    ("pipeline_failure", "Processing failed"),
    ("orphan_marker", "Incomplete submission"),
    ("phi_redacted", "PHI redacted"),
])
def test_display_for_type_returns_documented_label(item_type, expected_label):
    out = display_for_type(item_type)
    assert isinstance(out, TypeDisplay)
    assert out.label == expected_label
    # Description is non-empty, surgeon-facing copy.
    assert len(out.description) > 10


def test_display_for_type_unknown_returns_titlecased_fallback():
    """Generic fallback: title-case the raw type so e.g. ``foo_bar`` →
    ``"Foo Bar"`` — recognizable at a glance even before its UI mapping
    lands."""
    out = display_for_type("foo_bar_baz")
    assert out.label == "Foo Bar Baz"
    assert "No additional information" in out.description


def test_display_for_type_empty_string_falls_back_calmly():
    """Empty type string shouldn't crash; produces a generic Notice
    label so the card stays renderable."""
    out = display_for_type("")
    assert out.label
    assert out.description


# ----- table consistency -----


def test_action_and_display_tables_share_the_same_keys():
    """Catches a drift bug where adding to one table forgets the other.
    Phi_redacted is in both as the pre-wire; if they diverge, the
    surgeon UI would render an action button for an unmapped type or a
    label-less action.

    Brief #4: ``malformed_marker`` is display-only (admin-routed; no
    surgeon action surface), so the action table is a *subset* of the
    display table rather than identical. Any display-only types must
    be explicitly listed below — adding one to ``SURGEON_TYPE_DISPLAY``
    without updating either ``SURGEON_ACTION_BY_TYPE`` or this allow-list
    is the drift we want to catch."""
    _DISPLAY_ONLY = {"malformed_marker"}
    actionable_types = set(SURGEON_ACTION_BY_TYPE)
    displayed_types = set(SURGEON_TYPE_DISPLAY)
    # Every actionable type must have a display entry.
    assert actionable_types <= displayed_types
    # Every display-only type must be explicitly enumerated above.
    assert displayed_types - actionable_types == _DISPLAY_ONLY
