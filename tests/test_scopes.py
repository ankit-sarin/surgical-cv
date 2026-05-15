"""UserScope hierarchy tests — constructor wiring, listing stubs, in-scope vs
out-of-scope behavior, admin-only operations rejected for surgeons."""

from __future__ import annotations

import pytest

from app.exceptions import ScopeViolationError
from app.scopes import AdminScope, SurgeonScope, UserScope


# ----- constructors -----


def test_surgeon_scope_role_is_surgeon():
    s = SurgeonScope("asarin", "sarin")
    assert s.role == "surgeon"
    assert s.username == "asarin"
    assert s.folder_slug == "sarin"


def test_admin_scope_role_is_admin():
    s = AdminScope("ankitsarin")
    assert s.role == "admin"
    assert s.username == "ankitsarin"


def test_surgeon_scope_owned_case_ids_default_empty():
    s = SurgeonScope("asarin", "sarin")
    # Anything is out-of-scope.
    with pytest.raises(ScopeViolationError):
        s.read_case("UCD-FIL-001")


def test_surgeon_scope_accepts_owned_case_ids_iterable():
    s = SurgeonScope("asarin", "sarin", owned_case_ids=["UCD-FIL-001", "UCD-FIL-002"])
    # Owned case ids do NOT raise ScopeViolationError; they raise
    # NotImplementedError (the Spec B stub for the in-scope body).
    with pytest.raises(NotImplementedError):
        s.read_case("UCD-FIL-001")


def test_userscope_is_abstract():
    with pytest.raises(TypeError):
        UserScope()  # type: ignore[abstract]


# ----- listing methods (stubs return []) -----


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
def test_surgeon_listing_methods_return_empty(method):
    s = SurgeonScope("asarin", "sarin")
    assert getattr(s, method)() == []


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
def test_admin_listing_methods_return_empty(method):
    s = AdminScope("ankitsarin")
    assert getattr(s, method)() == []


# ----- surgeon: targeted methods raise scope violation for unowned case -----


@pytest.mark.parametrize(
    "method,args",
    [
        ("read_case", ("UCD-FIL-999",)),
        ("write_case_metadata", ("UCD-FIL-999",)),
        ("trigger_pipeline", ("UCD-FIL-999", "deid")),
    ],
)
def test_surgeon_targeted_out_of_scope_raises(method, args):
    s = SurgeonScope("asarin", "sarin", owned_case_ids=["UCD-FIL-001"])
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
    s = SurgeonScope("asarin", "sarin", owned_case_ids=["UCD-FIL-001"])
    with pytest.raises(NotImplementedError):
        getattr(s, method)(*args)


# ----- surgeon: admin-only methods always raise scope violation -----


def test_surgeon_resolve_audit_flag_always_raises():
    s = SurgeonScope("asarin", "sarin", owned_case_ids=["UCD-FIL-001"])
    with pytest.raises(ScopeViolationError) as exc:
        s.resolve_audit_flag(42)
    assert exc.value.resource == "audit_flag:42"
    assert exc.value.action == "resolve_audit_flag"
    assert exc.value.scope_at_time == "surgeon:sarin"


def test_surgeon_reupload_metadata_always_raises():
    s = SurgeonScope("asarin", "sarin", owned_case_ids=["UCD-FIL-001"])
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
    s = AdminScope("ankitsarin")
    with pytest.raises(NotImplementedError):
        getattr(s, method)(*args)


def test_admin_targeted_methods_never_raise_violation():
    """Even with case ids that look out-of-scope for a surgeon, admin passes."""
    s = AdminScope("ankitsarin")
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
    # Inherits Exception so it can be raised/caught.
    assert isinstance(exc, Exception)


def test_scope_violation_error_str_contains_action_and_resource():
    exc = ScopeViolationError("case:X", "read_case", "surgeon:sarin")
    assert "read_case" in str(exc)
    assert "case:X" in str(exc)


# ----- ScopeViolationError from a SurgeonScope method propagates to the
# central handler (integration test via TestClient) -----


def test_scope_violation_from_scope_method_returns_403(
    client, monkeypatch, app_env
):
    """Simulate a targeted scope-method violation reaching the request flow
    via a synthetic route that exercises the SurgeonScope. We don't have such
    a route in Spec B's placeholder /app — so test the handler directly with
    a one-off route attached at test time."""
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
        # Force a ScopeViolationError from inside the scope.
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
