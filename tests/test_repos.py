"""Tests for ``app/repos/cases.py`` — CsvCaseRepository against a tmpdir CSV
fixture, plus InMemoryCaseRepository's pure-Python behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.repos.cases import (
    CsvCaseRepository,
    InMemoryCaseRepository,
    manifest_path,
)


_HEADER = (
    "ucd_fil_id,surgeon,case_year,or_room,procedure_name,approach,indication,notes"
)


def _write_manifest(target: Path, rows: list[str]) -> Path:
    target.write_text(_HEADER + "\n" + "\n".join(rows) + "\n")
    return target


# ----- manifest_path() env override -----


def test_manifest_path_honors_env(monkeypatch, tmp_path):
    custom = tmp_path / "custom.csv"
    monkeypatch.setenv("CASE_MANIFEST_PATH", str(custom))
    assert manifest_path() == custom


def test_manifest_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("CASE_MANIFEST_PATH", raising=False)
    p = manifest_path()
    assert p.name == "case_manifest.csv"
    assert "/mnt/nas/or-raw" in str(p)


# ----- CsvCaseRepository — happy paths -----


def test_csv_list_owned_by_returns_matching_case_ids(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.csv",
        [
            "UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,",
            "UCD-FIL-002,sarin,2026,OR 4,Right hemicolectomy,Robotic,Colorectal cancer,",
            "UCD-FIL-099,miller,2026,OR 1,Sigmoidectomy,Open,Colorectal cancer,",
        ],
    )
    repo = CsvCaseRepository(manifest)
    assert sorted(repo.list_owned_by("sarin")) == ["UCD-FIL-001", "UCD-FIL-002"]
    assert repo.list_owned_by("miller") == ["UCD-FIL-099"]


def test_csv_list_owned_by_unknown_surgeon_returns_empty(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.csv",
        ["UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,"],
    )
    repo = CsvCaseRepository(manifest)
    assert repo.list_owned_by("ghost") == []


def test_csv_get_case_returns_dict_with_all_columns(tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.csv",
        ["UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,note-x"],
    )
    repo = CsvCaseRepository(manifest)
    case = repo.get_case("UCD-FIL-001")
    assert case is not None
    assert case["ucd_fil_id"] == "UCD-FIL-001"
    assert case["surgeon"] == "sarin"
    assert case["procedure_name"] == "Low anterior resection"
    assert case["notes"] == "note-x"


def test_csv_get_case_unknown_returns_none(tmp_path):
    manifest = _write_manifest(tmp_path / "m.csv", [])
    repo = CsvCaseRepository(manifest)
    assert repo.get_case("UCD-FIL-001") is None


@pytest.mark.parametrize(
    "case_id,folder,expected",
    [
        ("UCD-FIL-001", "sarin", True),
        ("UCD-FIL-001", "miller", False),
        ("UCD-FIL-999", "sarin", False),
    ],
)
def test_csv_case_belongs_to(tmp_path, case_id, folder, expected):
    manifest = _write_manifest(
        tmp_path / "m.csv",
        [
            "UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,",
            "UCD-FIL-099,miller,2026,OR 1,Sigmoidectomy,Open,Colorectal cancer,",
        ],
    )
    repo = CsvCaseRepository(manifest)
    assert repo.case_belongs_to(case_id, folder) is expected


# ----- CsvCaseRepository — missing file is empty, not error -----


def test_csv_missing_file_yields_empty_results(tmp_path):
    repo = CsvCaseRepository(tmp_path / "does-not-exist.csv")
    assert repo.list_owned_by("sarin") == []
    assert repo.get_case("UCD-FIL-001") is None
    assert repo.case_belongs_to("UCD-FIL-001", "sarin") is False


# ----- CsvCaseRepository — stateless re-read -----


def test_csv_reads_fresh_each_call(tmp_path):
    """Mutate the file between calls; the second call must see the new state."""
    manifest = _write_manifest(
        tmp_path / "m.csv",
        ["UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,"],
    )
    repo = CsvCaseRepository(manifest)
    assert repo.list_owned_by("sarin") == ["UCD-FIL-001"]

    _write_manifest(
        manifest,
        [
            "UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,",
            "UCD-FIL-002,sarin,2026,OR 4,Right hemicolectomy,Robotic,Colorectal cancer,",
        ],
    )
    assert sorted(repo.list_owned_by("sarin")) == ["UCD-FIL-001", "UCD-FIL-002"]


# ----- CsvCaseRepository — env-var-resolved path (no explicit constructor arg) -----


def test_csv_env_var_path(monkeypatch, tmp_path):
    manifest = _write_manifest(
        tmp_path / "m.csv",
        ["UCD-FIL-001,sarin,2026,OR 4,Low anterior resection,Robotic,Colorectal cancer,"],
    )
    monkeypatch.setenv("CASE_MANIFEST_PATH", str(manifest))
    repo = CsvCaseRepository()  # no explicit path → reads env
    assert repo.list_owned_by("sarin") == ["UCD-FIL-001"]


# ----- InMemoryCaseRepository -----


def test_inmem_list_owned_by():
    repo = InMemoryCaseRepository({
        "UCD-FIL-001": {"surgeon": "sarin"},
        "UCD-FIL-002": {"surgeon": "sarin"},
        "UCD-FIL-099": {"surgeon": "miller"},
    })
    assert sorted(repo.list_owned_by("sarin")) == ["UCD-FIL-001", "UCD-FIL-002"]
    assert repo.list_owned_by("miller") == ["UCD-FIL-099"]
    assert repo.list_owned_by("ghost") == []


def test_inmem_get_case_returns_copy():
    """Mutation of the returned dict must not leak back into the repo."""
    repo = InMemoryCaseRepository({"UCD-FIL-001": {"surgeon": "sarin"}})
    case = repo.get_case("UCD-FIL-001")
    case["surgeon"] = "tampered"
    again = repo.get_case("UCD-FIL-001")
    assert again["surgeon"] == "sarin"


def test_inmem_get_case_unknown():
    repo = InMemoryCaseRepository()
    assert repo.get_case("UCD-FIL-001") is None


@pytest.mark.parametrize(
    "case_id,folder,expected",
    [
        ("UCD-FIL-001", "sarin", True),
        ("UCD-FIL-001", "miller", False),
        ("UCD-FIL-999", "sarin", False),
    ],
)
def test_inmem_case_belongs_to(case_id, folder, expected):
    repo = InMemoryCaseRepository({
        "UCD-FIL-001": {"surgeon": "sarin"},
        "UCD-FIL-099": {"surgeon": "miller"},
    })
    assert repo.case_belongs_to(case_id, folder) is expected


def test_inmem_empty_by_default():
    repo = InMemoryCaseRepository()
    assert repo.list_owned_by("sarin") == []
    assert repo.get_case("UCD-FIL-001") is None
