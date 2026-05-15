"""UserScope hierarchy tests — constructor wiring, listing methods, in-scope
vs out-of-scope behavior, admin-only operations rejected for surgeons. Uses
``InMemoryCaseRepository`` for unit tests; the central-handler integration
test goes through ``build_scope`` and the conftest-seeded CSV manifest."""

from __future__ import annotations

import pytest

from app.exceptions import ScopeViolationError
from app.repos import (
    InMemoryAttentionItemsRepository,
    InMemoryCaseRepository,
    InMemoryPicklistRepository,
    InMemoryPipelineStateRepository,
    InMemoryRawSegmentRepository,
    Repos,
)
from app.scopes import AdminScope, SurgeonScope, UserScope


def _repos(
    case=None,
    segment=None,
    picklist=None,
    pipeline_state=None,
    attention=None,
) -> Repos:
    return Repos(
        case=case or InMemoryCaseRepository(),
        segment=segment or InMemoryRawSegmentRepository(),
        picklist=picklist or InMemoryPicklistRepository(),
        pipeline_state=pipeline_state or InMemoryPipelineStateRepository(),
        attention=attention or InMemoryAttentionItemsRepository(),
    )


def _empty_repo() -> Repos:
    return _repos()


def _sarin_repo(*case_ids: str) -> Repos:
    return _repos(
        case=InMemoryCaseRepository(
            {cid: {"surgeon": "sarin"} for cid in case_ids}
        )
    )


# ----- constructors -----


def test_surgeon_scope_role_is_surgeon():
    s = SurgeonScope("asarin", "sarin", _empty_repo())
    assert s.role == "surgeon"
    assert s.username == "asarin"
    assert s.folder_slug == "sarin"


def test_admin_scope_role_is_admin():
    s = AdminScope("ankitsarin", _empty_repo())
    assert s.role == "admin"
    assert s.username == "ankitsarin"


def test_surgeon_scope_empty_repo_treats_everything_as_out_of_scope():
    s = SurgeonScope("asarin", "sarin", _empty_repo())
    with pytest.raises(ScopeViolationError):
        s.read_case("UCD-FIL-001")


def test_surgeon_scope_in_scope_via_repo_raises_not_implemented():
    s = SurgeonScope("asarin", "sarin", _sarin_repo("UCD-FIL-001", "UCD-FIL-002"))
    with pytest.raises(NotImplementedError):
        s.read_case("UCD-FIL-001")


def test_userscope_is_abstract():
    with pytest.raises(TypeError):
        UserScope()  # type: ignore[abstract]


# ----- listing methods -----


@pytest.mark.parametrize(
    "method",
    [
        "list_concatted_masters",
        "list_deid_videos",
        "read_manifest_rows",
        "list_audit_queue",
    ],
)
def test_surgeon_case_listings_delegate_to_case_repo(method):
    s = SurgeonScope("asarin", "sarin", _sarin_repo("UCD-FIL-001", "UCD-FIL-002"))
    result = getattr(s, method)()
    assert sorted(result) == ["UCD-FIL-001", "UCD-FIL-002"]


@pytest.mark.parametrize(
    "method",
    [
        "list_concatted_masters",
        "list_deid_videos",
        "read_manifest_rows",
        "list_audit_queue",
    ],
)
def test_surgeon_case_listings_filter_by_folder_slug(method):
    repos = _repos(
        case=InMemoryCaseRepository({
            "UCD-FIL-001": {"surgeon": "sarin"},
            "UCD-FIL-099": {"surgeon": "miller"},
        })
    )
    s = SurgeonScope("asarin", "sarin", repos)
    assert getattr(s, method)() == ["UCD-FIL-001"]


def test_surgeon_list_raw_segments_delegates_to_segment_repo():
    """list_raw_segments routes through the segment repo, not the case repo."""
    from datetime import datetime, timezone

    from app.repos import SegmentRecord

    ts = datetime(2026, 1, 2, 8, 20, tzinfo=timezone.utc)
    seg = SegmentRecord(
        filename="capt0_20260102-082000.mp4",
        timestamp=ts,
        size_bytes=2_000_000_000,
        path=__import__("pathlib").Path("/tmp/raw-sarin/capt0_20260102-082000.mp4"),
    )
    repos = _repos(
        segment=InMemoryRawSegmentRepository({"sarin": [seg]})
    )
    s = SurgeonScope("asarin", "sarin", repos)
    result = s.list_raw_segments()
    assert result == [seg]


def test_surgeon_list_raw_segments_filters_by_folder():
    """Segment repo keyed by folder_slug; other folders' segments not seen."""
    from datetime import datetime, timezone

    from app.repos import SegmentRecord

    ts = datetime(2026, 1, 2, 8, 20, tzinfo=timezone.utc)
    sarin_seg = SegmentRecord(
        filename="capt0_20260102-082000.mp4",
        timestamp=ts,
        size_bytes=1,
        path=__import__("pathlib").Path("/tmp/raw-sarin/x.mp4"),
    )
    miller_seg = SegmentRecord(
        filename="capt0_20260102-090000.mp4",
        timestamp=ts,
        size_bytes=1,
        path=__import__("pathlib").Path("/tmp/raw-miller/x.mp4"),
    )
    repos = _repos(
        segment=InMemoryRawSegmentRepository(
            {"sarin": [sarin_seg], "miller": [miller_seg]}
        )
    )
    s = SurgeonScope("asarin", "sarin", repos)
    assert s.list_raw_segments() == [sarin_seg]


@pytest.mark.parametrize(
    "method",
    [
        "list_raw_segments",
        "list_concatted_masters",
        "list_deid_videos",
        "read_manifest_rows",
        "list_audit_queue",
    ],
)
def test_admin_listings_return_empty_for_now(method):
    """Admin pass-through is a Spec C stub returning []; future specs add
    ``repo.list_all()`` and call it here."""
    repo = InMemoryCaseRepository({"UCD-FIL-001": {"surgeon": "sarin"}})
    s = AdminScope("ankitsarin", repo)
    assert getattr(s, method)() == []


# ----- surgeon: targeted methods raise scope violation for unowned cases -----


@pytest.mark.parametrize(
    "method,args",
    [
        ("read_case", ("UCD-FIL-999",)),
        ("write_case_metadata", ("UCD-FIL-999",)),
        ("trigger_pipeline", ("UCD-FIL-999", "deid")),
    ],
)
def test_surgeon_targeted_out_of_scope_raises(method, args):
    s = SurgeonScope("asarin", "sarin", _sarin_repo("UCD-FIL-001"))
    with pytest.raises(ScopeViolationError) as exc:
        getattr(s, method)(*args)
    assert exc.value.resource == "case:UCD-FIL-999"
    assert exc.value.scope_at_time == "surgeon:sarin"


@pytest.mark.parametrize(
    "method,args",
    [
        ("read_case", ("UCD-FIL-001",)),
        ("write_case_metadata", ("UCD-FIL-001",)),
        ("trigger_pipeline", ("UCD-FIL-001", "deid")),
    ],
)
def test_surgeon_targeted_in_scope_raises_not_implemented(method, args):
    s = SurgeonScope("asarin", "sarin", _sarin_repo("UCD-FIL-001"))
    with pytest.raises(NotImplementedError):
        getattr(s, method)(*args)


# ----- surgeon: admin-only methods always raise scope violation -----


def test_surgeon_resolve_audit_flag_always_raises():
    s = SurgeonScope("asarin", "sarin", _sarin_repo("UCD-FIL-001"))
    with pytest.raises(ScopeViolationError) as exc:
        s.resolve_audit_flag(42)
    assert exc.value.resource == "audit_flag:42"
    assert exc.value.action == "resolve_audit_flag"
    assert exc.value.scope_at_time == "surgeon:sarin"


def test_surgeon_reupload_metadata_always_raises():
    s = SurgeonScope("asarin", "sarin", _sarin_repo("UCD-FIL-001"))
    with pytest.raises(ScopeViolationError) as exc:
        s.reupload_metadata("UCD-FIL-001")
    assert exc.value.resource == "case:UCD-FIL-001"
    assert exc.value.action == "reupload_metadata"


# ----- admin: all targeted methods raise NotImplementedError (Spec C stub) -----


@pytest.mark.parametrize(
    "method,args",
    [
        ("read_case", ("UCD-FIL-001",)),
        ("write_case_metadata", ("UCD-FIL-001",)),
        ("trigger_pipeline", ("UCD-FIL-001", "deid")),
        ("resolve_audit_flag", (42,)),
        ("reupload_metadata", ("UCD-FIL-001",)),
    ],
)
def test_admin_targeted_methods_raise_not_implemented(method, args):
    s = AdminScope("ankitsarin", _empty_repo())
    with pytest.raises(NotImplementedError):
        getattr(s, method)(*args)


def test_admin_targeted_methods_never_raise_violation():
    """Even with case ids absent from the repo, admin pass-through still
    reaches the NotImplementedError stub rather than raising a violation."""
    s = AdminScope("ankitsarin", _empty_repo())
    for case_id in ("UCD-FIL-001", "UCD-FIL-999", "completely-made-up-case"):
        with pytest.raises(NotImplementedError):
            s.read_case(case_id)


# ----- ScopeViolationError shape -----


def test_scope_violation_error_attrs():
    exc = ScopeViolationError(
        resource="case:UCD-FIL-007",
        action="read_case",
        scope_at_time="surgeon:miller",
    )
    assert exc.resource == "case:UCD-FIL-007"
    assert exc.action == "read_case"
    assert exc.scope_at_time == "surgeon:miller"
    assert isinstance(exc, Exception)


def test_scope_violation_error_str_contains_action_and_resource():
    exc = ScopeViolationError("case:X", "read_case", "surgeon:sarin")
    assert "read_case" in str(exc)
    assert "case:X" in str(exc)


# ----- end-to-end: ScopeViolationError from SurgeonScope.read_case routes
# through the central handler (200 OK never returned; 403 + violation log) -----


def test_scope_violation_from_scope_method_returns_403(
    client, monkeypatch, app_env
):
    """The build_scope dependency uses CsvCaseRepository against the conftest
    CASE_MANIFEST_PATH CSV. asarin (folder_slug=sarin) tries to read a case
    that isn't in the seeded manifest → SurgeonScope.read_case raises
    ScopeViolationError → central handler logs + returns generic 403."""
    from fastapi import Depends

    from app.auth import current_user_required
    from app.main import app as fastapi_app
    from app.main import build_scope
    from tests.conftest import patch_dsm, read_violations

    @fastapi_app.get("/_test/violation/{case_id}")
    async def _violation_probe(
        case_id: str,
        scope=Depends(build_scope),
        user=Depends(current_user_required),
    ):
        if user["role"] != "surgeon":
            return {"ok": True}
        scope.read_case(case_id)
        return {"ok": True}

    patch_dsm(monkeypatch, {"success": True})
    client.post(
        "/login", data={"username": "asarin", "password": "x"},
        follow_redirects=False,
    )

    r = client.get("/_test/violation/UCD-FIL-NOT-OWNED")
    assert r.status_code == 403
    assert "Forbidden" in r.text

    rows = read_violations(app_env)
    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "asarin"
    assert row["attempted_resource"] == "case:UCD-FIL-NOT-OWNED"
    assert row["attempted_action"] == "read_case"
    assert row["scope_at_time"] == "surgeon:sarin"
