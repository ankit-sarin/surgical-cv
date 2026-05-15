"""Picklist loader for structured vocabularies (field × specialty seed files).

Reads seed files from ``app/db/seeds/picklists/`` (override via the
``PIPELINE_PICKLIST_DIR`` env var). File naming convention:

    {field}_{specialty}.json   when a specialty is given
    {field}.json               when specialty is None (future universal lists)

File schema (frozen):

    {
      "field": "<field-name>",
      "specialty": "<specialty>" | null,
      "values": [
        {"value": "...", "display_label": "...", "sort_order": <int>},
        ...
      ]
    }

Two entry points:

- ``load_picklist(field, specialty)`` returns the full structured records,
  sorted by ``sort_order``.
- ``load_picklist_values(field, specialty)`` returns the flat list of value
  strings, sorted by ``sort_order``. Used by call sites that want a drop-in
  replacement for a hand-maintained Python list.

Results are cached at module load by resolved file path so that env-var
changes between callers do not return stale entries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_PICKLIST_SUBPATH = ("app", "db", "seeds", "picklists")

_cache: dict[str, list[dict[str, Any]]] = {}


class PicklistError(Exception):
    """Seed file missing, unreadable, or malformed."""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _picklist_dir() -> Path:
    env = os.environ.get("PIPELINE_PICKLIST_DIR")
    if env:
        return Path(env)
    return _project_root().joinpath(*_DEFAULT_PICKLIST_SUBPATH)


def _resolve_path(field: str, specialty: str | None) -> Path:
    name = f"{field}_{specialty}.json" if specialty else f"{field}.json"
    return _picklist_dir() / name


def _read_records(path: Path) -> list[dict[str, Any]]:
    key = str(path)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    if not path.exists():
        raise PicklistError(f"picklist seed file missing: {path}")
    try:
        text = path.read_text()
    except OSError as e:
        raise PicklistError(f"picklist seed file unreadable at {path}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PicklistError(f"picklist seed file malformed at {path}: {e}") from e

    if not isinstance(data, dict) or "values" not in data:
        raise PicklistError(
            f"picklist seed file at {path} must be an object with a 'values' array"
        )
    values = data["values"]
    if not isinstance(values, list):
        raise PicklistError(
            f"picklist seed file at {path}: 'values' must be a list"
        )
    for i, item in enumerate(values):
        if not isinstance(item, dict):
            raise PicklistError(
                f"picklist seed file at {path}: values[{i}] must be an object"
            )
        for required in ("value", "display_label", "sort_order"):
            if required not in item:
                raise PicklistError(
                    f"picklist seed file at {path}: values[{i}] missing '{required}'"
                )
        if not isinstance(item["value"], str) or not isinstance(item["display_label"], str):
            raise PicklistError(
                f"picklist seed file at {path}: values[{i}] 'value'/'display_label' must be strings"
            )
        if not isinstance(item["sort_order"], int) or isinstance(item["sort_order"], bool):
            raise PicklistError(
                f"picklist seed file at {path}: values[{i}] 'sort_order' must be int"
            )

    records = sorted(values, key=lambda r: r["sort_order"])
    _cache[key] = records
    return records


def load_picklist(field: str, specialty: str | None) -> list[dict[str, Any]]:
    """Return structured picklist records sorted by sort_order."""
    return _read_records(_resolve_path(field, specialty))


def load_picklist_values(field: str, specialty: str | None) -> list[str]:
    """Return the flat list of value strings sorted by sort_order."""
    return [r["value"] for r in load_picklist(field, specialty)]
