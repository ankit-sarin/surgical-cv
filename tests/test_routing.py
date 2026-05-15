"""Routing tests: role-prefix enforcement, redirects, generic 403 body,
scope_violation_log writes."""

from __future__ import annotations

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


# ----- / (root) -----


def test_root_redirects_unauthenticated_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_root_redirects_surgeon_to_app(client, monkeypatch):
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app"


def test_root_redirects_admin_to_admin(client, monkeypatch):
    _login_as(client, monkeypatch, "ankitsarin")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


# ----- /app prefix -----


def test_app_prefix_requires_authentication(client):
    r = client.get("/app")
    assert r.status_code == 401


def test_surgeon_can_access_app_prefix(client, monkeypatch):
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/app")
    assert r.status_code == 200
    assert "Surgeon Home" in r.text
    assert "asarin" in r.text


def test_admin_blocked_from_app_prefix(client, monkeypatch, app_env):
    _login_as(client, monkeypatch, "ankitsarin")
    r = client.get("/app")
    assert r.status_code == 403
    # Generic body — no role names leaked.
    assert "Forbidden" in r.text
    assert "surgeon" not in r.text.lower()
    assert "admin" not in r.text.lower()


def test_admin_to_app_logs_violation(client, monkeypatch, app_env):
    _login_as(client, monkeypatch, "ankitsarin")
    client.get("/app")
    rows = read_violations(app_env)
    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "ankitsarin"
    assert row["attempted_resource"] == "/app"
    assert row["attempted_action"] == "GET"
    assert row["scope_at_time"] == "admin"
    assert row["created_at"]


# ----- /admin prefix -----


def test_admin_prefix_requires_authentication(client):
    r = client.get("/admin")
    assert r.status_code == 401


def test_admin_can_access_admin_prefix(client, monkeypatch):
    _login_as(client, monkeypatch, "ankitsarin")
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Admin Home" in r.text


def test_surgeon_blocked_from_admin_prefix(client, monkeypatch, app_env):
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/admin")
    assert r.status_code == 403
    assert "Forbidden" in r.text
    assert "surgeon" not in r.text.lower()
    assert "admin" not in r.text.lower()


def test_surgeon_to_admin_logs_violation(client, monkeypatch, app_env):
    _login_as(client, monkeypatch, "asarin")
    client.get("/admin")
    rows = read_violations(app_env)
    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "asarin"
    assert row["attempted_resource"] == "/admin"
    assert row["attempted_action"] == "GET"
    assert row["scope_at_time"] == "surgeon:sarin"


# ----- violation log shape -----


def test_violation_records_user_agent(client, monkeypatch, app_env):
    _login_as(client, monkeypatch, "asarin")
    client.get("/admin", headers={"user-agent": "pytest-fakeagent/9.9"})
    rows = read_violations(app_env)
    assert rows[-1]["user_agent"] == "pytest-fakeagent/9.9"


def test_no_violation_for_legitimate_access(client, monkeypatch, app_env):
    _login_as(client, monkeypatch, "asarin")
    client.get("/app")
    rows = read_violations(app_env)
    assert rows == []


def test_unauth_request_does_not_log_violation(client, app_env):
    client.get("/admin")
    client.get("/app")
    rows = read_violations(app_env)
    assert rows == []


# ----- 403 body never names the other role -----


def test_admin_to_app_body_does_not_mention_surgeon(client, monkeypatch):
    _login_as(client, monkeypatch, "ankitsarin")
    r = client.get("/app")
    assert "surgeon" not in r.text.lower()


def test_surgeon_to_admin_body_does_not_mention_admin(client, monkeypatch):
    _login_as(client, monkeypatch, "asarin")
    r = client.get("/admin")
    assert "admin" not in r.text.lower()


# ----- session expiry behavior at protected route -----


def test_protected_route_after_session_expiry(client, monkeypatch):
    _login_as(client, monkeypatch, "asarin")
    # Force session expiry.
    monkeypatch.setattr("app.auth.SESSION_MAX_AGE_S", -1)
    r = client.get("/app")
    assert r.status_code == 401
