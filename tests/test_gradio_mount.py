"""Gradio mount tests: surgeon /app/ and admin /admin/ render their respective
shells under correct auth; mount order vs role enforcement is correct (cross-
role still 403 — request never reaches Gradio); identity helper renders the
expected ``Signed in as …`` string under various session states."""

from __future__ import annotations

import sqlite3
import types

from app.auth import (
    SESSION_COOKIE_NAME,
    encode_session,
    identity_string_for_request,
)
from tests.conftest import patch_dsm, read_violations


def _login_as(client, monkeypatch, username):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": username, "password": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r


def _fake_request(cookies: dict | None = None):
    return types.SimpleNamespace(cookies=cookies or {})


# ----- mount: surgeon /app/ -----


def test_surgeon_at_app_renders_gradio_shell(client, monkeypatch):
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/app/")
    assert r.status_code == 200
    assert "gradio" in r.text.lower()


def test_admin_at_app_blocked_before_reaching_gradio(
    client, monkeypatch, app_env
):
    """Cross-role: admin requesting /app/ must be rejected by the auth_dep
    before Gradio handles anything. 403 + violation log row, no gradio
    markup in the response."""
    _login_as(client, monkeypatch, "ankitsarin")
    r = client.get("/app/")
    assert r.status_code == 403
    # The 403 body is generic — no Gradio shell rendered.
    assert "gradio" not in r.text.lower()
    rows = read_violations(app_env)
    assert len(rows) == 1
    assert rows[0]["username"] == "ankitsarin"


def test_unauth_at_app_returns_401(client):
    r = client.get("/app/")
    assert r.status_code == 401


# ----- mount: admin /admin/ -----


def test_admin_at_admin_renders_gradio_shell(client, monkeypatch):
    _login_as(client, monkeypatch, "ankitsarin")
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "gradio" in r.text.lower()


def test_surgeon_at_admin_blocked_before_reaching_gradio(
    client, monkeypatch, app_env
):
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/admin/")
    assert r.status_code == 403
    assert "gradio" not in r.text.lower()
    rows = read_violations(app_env)
    assert len(rows) == 1
    assert rows[0]["username"] == "asarin"
    assert rows[0]["scope_at_time"] == "surgeon:sarin"


def test_unauth_at_admin_returns_401(client):
    r = client.get("/admin/")
    assert r.status_code == 401


# ----- identity helper -----


def test_identity_for_surgeon_renders_folder_and_specialty(app_env):
    token = encode_session("asarin")
    req = _fake_request({SESSION_COOKIE_NAME: token})
    assert identity_string_for_request(req) == (
        "Signed in as asarin (sarin, colorectal)"
    )


def test_identity_for_admin_renders_admin_and_all_specialties(app_env):
    token = encode_session("ankitsarin")
    req = _fake_request({SESSION_COOKIE_NAME: token})
    # Admin has folder_slug=NULL, specialty=NULL → "admin" / "all specialties".
    assert identity_string_for_request(req) == (
        "Signed in as ankitsarin (admin, all specialties)"
    )


def test_identity_uses_display_name_when_set(app_env):
    # Patch the seeded user to have a display_name.
    conn = sqlite3.connect(app_env)
    conn.execute(
        "UPDATE users SET display_name = ? WHERE username = ?",
        ("Ankit Sarin, MD", "asarin"),
    )
    conn.commit()
    conn.close()

    token = encode_session("asarin")
    req = _fake_request({SESSION_COOKIE_NAME: token})
    assert identity_string_for_request(req) == (
        "Signed in as Ankit Sarin, MD (sarin, colorectal)"
    )


def test_identity_fallback_with_no_cookie(app_env):
    assert identity_string_for_request(_fake_request({})) == "Signed in."


def test_identity_fallback_with_garbage_cookie(app_env):
    req = _fake_request({SESSION_COOKIE_NAME: "garbage.not.a.token"})
    assert identity_string_for_request(req) == "Signed in."


def test_identity_fallback_with_unknown_user(app_env):
    # Token signed for a user that isn't in the DB.
    token = encode_session("nonexistent-user")
    req = _fake_request({SESSION_COOKIE_NAME: token})
    assert identity_string_for_request(req) == "Signed in."


def test_identity_fallback_with_inactive_user(app_env):
    # conftest seeds 'inactiveuser' with active=0.
    token = encode_session("inactiveuser")
    req = _fake_request({SESSION_COOKIE_NAME: token})
    assert identity_string_for_request(req) == "Signed in."


def test_identity_fallback_when_request_is_none(app_env):
    assert identity_string_for_request(None) == "Signed in."


# ----- Blocks construction: tabs exist and carry the expected labels -----


def test_surgeon_blocks_carries_three_tabs():
    from app.surgeon_app import build_surgeon_app

    blocks = build_surgeon_app()
    labels = _collect_tab_labels(blocks)
    assert labels == ["Intake", "My Cases", "Action Required"]


def test_admin_blocks_carries_two_tabs():
    from app.admin_app import build_admin_app

    blocks = build_admin_app()
    labels = _collect_tab_labels(blocks)
    assert labels == ["Global Dashboard", "Action Required"]


def _collect_tab_labels(blocks) -> list[str]:
    """Walk the Blocks component tree and collect ``gr.Tab`` labels in order."""
    import gradio as gr

    labels: list[str] = []
    for component in blocks.blocks.values():
        if isinstance(component, gr.Tab):
            labels.append(component.label)
    return labels
