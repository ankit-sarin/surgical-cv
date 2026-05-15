"""Picklist repository — read-only access to ``picklist_values`` in app.db.

The Intake form's dropdowns / radios pull their choices from here. v1 backing
is a direct ``sqlite3`` read via ``app.db.connection.connect()``; no caching
since SQLite reads are O(microseconds) and the call sites are page-load
events rather than hot loops.

``list_active(field, specialty)`` combines two scopes per the v18 design:

- ``specialty=None``    → rows where ``picklist_values.specialty IS NULL``
                          (universal lists — approach, case_year).
- ``specialty="colorectal"`` → rows where specialty IS NULL **or** ==
                          "colorectal". Universals are visible to every
                          specialty so the surgeon's dropdown for an
                          approach picklist returns Open/Laparoscopic/etc.
                          regardless of which specialty they belong to.

``active=0`` rows are always excluded. Results are sorted by ``sort_order``.

``InMemoryPicklistRepository`` is the test fake — initialized with a flat
list of row dicts; same filter semantics, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from app.db.connection import connect


@dataclass(frozen=True)
class PicklistValue:
    value: str
    display_label: str
    sort_order: int


class PicklistRepository(Protocol):
    def list_active(
        self, field: str, specialty: str | None
    ) -> list[PicklistValue]: ...


class SqlitePicklistRepository:
    def list_active(
        self, field: str, specialty: str | None
    ) -> list[PicklistValue]:
        conn = connect()
        try:
            if specialty is None:
                rows = conn.execute(
                    "SELECT value, display_label, sort_order "
                    "FROM picklist_values "
                    "WHERE field = ? AND specialty IS NULL AND active = 1 "
                    "ORDER BY sort_order",
                    (field,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT value, display_label, sort_order "
                    "FROM picklist_values "
                    "WHERE field = ? "
                    "  AND (specialty IS NULL OR specialty = ?) "
                    "  AND active = 1 "
                    "ORDER BY sort_order",
                    (field, specialty),
                ).fetchall()
        finally:
            conn.close()
        return [
            PicklistValue(
                value=r["value"],
                display_label=r["display_label"],
                sort_order=r["sort_order"],
            )
            for r in rows
        ]


class InMemoryPicklistRepository:
    """Dict-backed test fake. Rows are dicts with keys
    {field, value, display_label, sort_order, specialty, active}.
    ``active`` defaults to True; ``specialty`` defaults to None."""

    def __init__(self, rows: Iterable[dict] | None = None):
        self._rows: list[dict] = []
        for r in rows or ():
            self._rows.append(
                {
                    "field": r["field"],
                    "value": r["value"],
                    "display_label": r.get("display_label", r["value"]),
                    "sort_order": r.get("sort_order", 0),
                    "specialty": r.get("specialty"),
                    "active": r.get("active", True),
                }
            )

    def list_active(
        self, field: str, specialty: str | None
    ) -> list[PicklistValue]:
        out: list[PicklistValue] = []
        for r in self._rows:
            if r["field"] != field or not r["active"]:
                continue
            row_specialty = r["specialty"]
            if specialty is None:
                if row_specialty is not None:
                    continue
            else:
                if row_specialty is not None and row_specialty != specialty:
                    continue
            out.append(
                PicklistValue(
                    value=r["value"],
                    display_label=r["display_label"],
                    sort_order=r["sort_order"],
                )
            )
        out.sort(key=lambda p: p.sort_order)
        return out
