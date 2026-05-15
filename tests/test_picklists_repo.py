"""Tests for ``app/repos/picklists.py`` — the SQLite-backed picklist reader
against the conftest-seeded test DB, plus the in-memory fake."""

from __future__ import annotations

import sqlite3

import pytest

from app.repos.picklists import (
    InMemoryPicklistRepository,
    PicklistValue,
    SqlitePicklistRepository,
)


# ----- SqlitePicklistRepository against the conftest-seeded DB -----


def test_list_active_returns_colorectal_procedures(app_env):
    repo = SqlitePicklistRepository()
    rows = repo.list_active("procedure", "colorectal")
    values = [r.value for r in rows]
    # Conftest seeds 4 active colorectal procedures (+1 inactive that must
    # be filtered out).
    assert values == [
        "Right hemicolectomy",
        "Sigmoidectomy",
        "Low anterior resection",
        "Other",
    ]


def test_list_active_excludes_inactive_rows(app_env):
    repo = SqlitePicklistRepository()
    rows = repo.list_active("procedure", "colorectal")
    assert "Deprecated proc" not in [r.value for r in rows]


def test_list_active_sorts_by_sort_order(app_env):
    repo = SqlitePicklistRepository()
    rows = repo.list_active("procedure", "colorectal")
    sort_orders = [r.sort_order for r in rows]
    assert sort_orders == sorted(sort_orders)


def test_list_active_universals_returned_when_specialty_is_none(app_env):
    repo = SqlitePicklistRepository()
    rows = repo.list_active("approach", None)
    assert [r.value for r in rows] == [
        "Open",
        "Laparoscopic",
        "Robotic",
        "Hybrid",
    ]


def test_list_active_universals_visible_to_colorectal(app_env):
    """approach has specialty=NULL rows; a colorectal-scoped query must see
    them (universal lists are visible to every specialty)."""
    repo = SqlitePicklistRepository()
    rows = repo.list_active("approach", "colorectal")
    assert {r.value for r in rows} == {"Open", "Laparoscopic", "Robotic", "Hybrid"}


def test_list_active_specialty_none_excludes_scoped_rows(app_env):
    """A universal query for ``procedure`` (which is specialty=colorectal in
    the seed) must return [] — universal-scope must not leak scoped rows."""
    repo = SqlitePicklistRepository()
    rows = repo.list_active("procedure", None)
    assert rows == []


def test_list_active_unknown_field_returns_empty(app_env):
    repo = SqlitePicklistRepository()
    assert repo.list_active("not_a_real_field", "colorectal") == []


def test_list_active_unknown_specialty_returns_universals_only(app_env):
    """A specialty code with no rows in the DB still gets the universal
    rows back — universals are by definition cross-specialty."""
    repo = SqlitePicklistRepository()
    rows = repo.list_active("approach", "made-up-specialty")
    assert len(rows) == 4  # all four approaches are universal


def test_list_active_returns_picklist_value_dataclass(app_env):
    repo = SqlitePicklistRepository()
    rows = repo.list_active("approach", None)
    assert all(isinstance(r, PicklistValue) for r in rows)
    assert rows[0].value == "Open"
    assert rows[0].display_label == "Open"
    assert rows[0].sort_order == 10


# ----- InMemoryPicklistRepository -----


def test_inmem_empty_by_default():
    repo = InMemoryPicklistRepository()
    assert repo.list_active("procedure", "colorectal") == []
    assert repo.list_active("approach", None) == []


def test_inmem_active_filter():
    repo = InMemoryPicklistRepository([
        {"field": "procedure", "value": "Sigmoidectomy",
         "specialty": "colorectal", "sort_order": 10, "active": True},
        {"field": "procedure", "value": "Old proc",
         "specialty": "colorectal", "sort_order": 5, "active": False},
    ])
    rows = repo.list_active("procedure", "colorectal")
    assert [r.value for r in rows] == ["Sigmoidectomy"]


def test_inmem_universal_visible_to_specialty():
    repo = InMemoryPicklistRepository([
        {"field": "approach", "value": "Open", "specialty": None, "sort_order": 10},
        {"field": "approach", "value": "Robotic", "specialty": None, "sort_order": 20},
    ])
    rows = repo.list_active("approach", "colorectal")
    assert [r.value for r in rows] == ["Open", "Robotic"]


def test_inmem_specialty_none_excludes_scoped():
    repo = InMemoryPicklistRepository([
        {"field": "procedure", "value": "Sigmoidectomy",
         "specialty": "colorectal", "sort_order": 10},
    ])
    assert repo.list_active("procedure", None) == []


def test_inmem_sorts_by_sort_order():
    repo = InMemoryPicklistRepository([
        {"field": "x", "value": "C", "specialty": None, "sort_order": 30},
        {"field": "x", "value": "A", "specialty": None, "sort_order": 10},
        {"field": "x", "value": "B", "specialty": None, "sort_order": 20},
    ])
    rows = repo.list_active("x", None)
    assert [r.value for r in rows] == ["A", "B", "C"]


def test_inmem_field_filter():
    repo = InMemoryPicklistRepository([
        {"field": "approach", "value": "Open", "specialty": None, "sort_order": 10},
        {"field": "indication", "value": "Cancer", "specialty": None, "sort_order": 10},
    ])
    assert [r.value for r in repo.list_active("approach", None)] == ["Open"]
    assert [r.value for r in repo.list_active("indication", None)] == ["Cancer"]
