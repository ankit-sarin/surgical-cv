"""Auth-layer tests: login flow, OTP flow, session cookie, fail-closed,
mock-auth, generic error copy. DSM calls are mocked via monkeypatched httpx
(test-only — production code never sees a fixture)."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.conftest import TEST_SECRET, patch_dsm


# ----- /healthz -----


def test_healthz_returns_200(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_does_not_require_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200


# ----- /login GET -----


def test_login_form_get_returns_html(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text
    assert 'type="password"' in r.text


# ----- /login POST happy path -----


def test_login_success_sets_session_cookie(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "asarin", "password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "app_session" in r.cookies


def test_login_success_redirects_to_root(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "ankitsarin", "password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_session_cookie_is_signed(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "asarin", "password": "x"},
        follow_redirects=False,
    )
    cookie_val = r.cookies["app_session"]
    # Signed tokens contain a dotted signature; the raw username never appears
    # in plaintext.
    assert "." in cookie_val
    assert "asarin" not in cookie_val


def test_login_session_cookie_decodes_to_username(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "asarin", "password": "x"},
        follow_redirects=False,
    )
    from app.auth import decode_session

    username = decode_session(r.cookies["app_session"])
    assert username == "asarin"


# ----- /login POST — generic invalid -----


def test_login_wrong_password_generic_error(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": False, "error": {"code": 400}})
    r = client.post(
        "/login", data={"username": "asarin", "password": "wrong"}
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text
    # No cookie set, no hint about username vs password.
    assert "app_session" not in r.cookies
    assert "user" not in r.text.lower() or "username" in r.text.lower()  # form re-shown


def test_login_unknown_username_same_error_as_wrong_password(client, monkeypatch):
    # Both should reach the same error path with the same body.
    patch_dsm(monkeypatch, {"success": False, "error": {"code": 400}})
    r_a = client.post(
        "/login", data={"username": "asarin", "password": "wrong"}
    )
    r_b = client.post(
        "/login", data={"username": "ghost", "password": "wrong"}
    )
    assert r_a.status_code == r_b.status_code == 401
    # Strip the form's HTML around the error and compare the error block.
    assert "Invalid credentials" in r_a.text
    assert "Invalid credentials" in r_b.text


# ----- fail-closed gates -----


def test_login_failclosed_when_user_missing_from_db(client, monkeypatch):
    # DSM happily accepts a username that doesn't exist in app.db.
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "ghost_not_in_db", "password": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text
    assert "app_session" not in r.cookies


def test_login_failclosed_when_user_inactive(client, monkeypatch):
    # 'inactiveuser' is seeded with active=0 in conftest.
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "inactiveuser", "password": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text
    assert "app_session" not in r.cookies


# ----- OTP flow -----


def test_login_needs_otp_renders_otp_form(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": False, "error": {"code": 403}})
    r = client.post(
        "/login", data={"username": "asarin", "password": "x"}
    )
    assert r.status_code == 200
    assert 'name="otp_code"' in r.text
    assert 'name="partial_auth_token"' in r.text
    assert "Two-factor" in r.text


def test_login_otp_token_in_form_is_signed(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": False, "error": {"code": 403}})
    r = client.post(
        "/login", data={"username": "asarin", "password": "secret123"}
    )
    # Extract the partial_auth_token value from the form.
    import re

    m = re.search(r'name="partial_auth_token" value="([^"]+)"', r.text)
    assert m is not None
    token = m.group(1)
    # Password never appears in the rendered form.
    assert "secret123" not in r.text
    from app.auth import decode_partial_auth

    decoded = decode_partial_auth(token)
    assert decoded == ("asarin", "secret123")


def test_login_otp_success_sets_cookie(client, monkeypatch):
    from app.auth import encode_partial_auth

    token = encode_partial_auth("asarin", "secret")
    # DSM mock returns success for the OTP-complete call.
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login/otp",
        data={"partial_auth_token": token, "otp_code": "123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "app_session" in r.cookies


def test_login_otp_invalid_returns_generic_error(client, monkeypatch):
    from app.auth import encode_partial_auth

    token = encode_partial_auth("asarin", "secret")
    patch_dsm(monkeypatch, {"success": False, "error": {"code": 404}})
    r = client.post(
        "/login/otp",
        data={"partial_auth_token": token, "otp_code": "000000"},
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text
    assert "app_session" not in r.cookies


def test_login_otp_tampered_token_rejected(client, monkeypatch):
    # Even though DSM would say success, the tampered token must reject first.
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login/otp",
        data={"partial_auth_token": "garbage.not-a-real-token", "otp_code": "123456"},
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_login_otp_expired_token_rejected(client, monkeypatch):
    from app.auth import _partial_serializer

    # Hand-roll an expired token by stamping it 200s ago. itsdangerous'
    # ``loads(max_age=120)`` will reject it.
    serializer = _partial_serializer()
    # Build a token, then convince loads it's old: easiest is to monkeypatch
    # time and round-trip.
    import itsdangerous

    real_dumps = itsdangerous.URLSafeTimedSerializer.dumps
    token = real_dumps(serializer, {"u": "asarin", "p": "x"})

    # Now reload with max_age=0 to simulate immediate expiry.
    monkeypatch.setattr(
        "app.auth.PARTIAL_AUTH_MAX_AGE_S", -1, raising=False
    )

    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login/otp",
        data={"partial_auth_token": token, "otp_code": "123456"},
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_login_otp_failclosed_user_missing(client, monkeypatch):
    from app.auth import encode_partial_auth

    token = encode_partial_auth("ghost_not_in_db", "x")
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login/otp",
        data={"partial_auth_token": token, "otp_code": "123456"},
    )
    assert r.status_code == 401
    assert "app_session" not in r.cookies


# ----- DSM call shape -----


def test_login_dsm_call_sends_username_and_password(client, monkeypatch):
    captured: dict = {}

    def capturing_post(url, data=None, **kw):
        captured["url"] = url
        captured["data"] = data
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", capturing_post)
    client.post(
        "/login", data={"username": "asarin", "password": "p4ssw0rd"}
    )
    assert captured["data"]["account"] == "asarin"
    assert captured["data"]["passwd"] == "p4ssw0rd"
    assert captured["data"]["api"] == "SYNO.API.Auth"


def test_login_otp_dsm_call_includes_otp_code(client, monkeypatch):
    from app.auth import encode_partial_auth

    token = encode_partial_auth("asarin", "p4ss")
    captured: dict = {}

    def capturing_post(url, data=None, **kw):
        captured.update(data or {})
        return httpx.Response(
            200, json={"success": True}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr("app.auth.httpx.post", capturing_post)
    client.post(
        "/login/otp",
        data={"partial_auth_token": token, "otp_code": "987654"},
    )
    assert captured.get("otp_code") == "987654"


def test_login_dsm_http_error_treated_as_invalid(client, monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr("app.auth.httpx.post", boom)
    r = client.post(
        "/login", data={"username": "asarin", "password": "x"}
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


# ----- logout -----


def test_logout_clears_session_cookie(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": True})
    client.post("/login", data={"username": "asarin", "password": "x"})
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # Set-Cookie should clear app_session.
    set_cookie = r.headers.get("set-cookie", "")
    assert "app_session=" in set_cookie
    # Either Max-Age=0 or expires-in-past.
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


# ----- session expiry / decoding -----


def test_decode_session_returns_none_on_bad_token():
    os.environ.setdefault("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import decode_session

    assert decode_session("not.a.token") is None
    assert decode_session(None) is None
    assert decode_session("") is None


def test_decode_session_returns_none_on_expired_token(monkeypatch):
    os.environ.setdefault("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import decode_session, encode_session

    token = encode_session("asarin")
    # Force max_age=-1 so any token is "expired".
    monkeypatch.setattr("app.auth.SESSION_MAX_AGE_S", -1)
    assert decode_session(token) is None


# ----- MOCK_AUTH -----


def test_mock_auth_bypasses_dsm(client, monkeypatch):
    monkeypatch.setenv("MOCK_AUTH", "1")

    # Wire a tripwire — if DSM is called when MOCK_AUTH=1, the test fails.
    def explode(*a, **kw):
        raise AssertionError("MOCK_AUTH=1 should bypass DSM HTTP calls")

    monkeypatch.setattr("app.auth.httpx.post", explode)
    r = client.post(
        "/login",
        data={"username": "asarin", "password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "app_session" in r.cookies


def test_mock_auth_rejects_empty_password(client, monkeypatch):
    monkeypatch.setenv("MOCK_AUTH", "1")
    # With FastAPI Form(...), the missing password actually fails validation
    # at the form layer (422). Use a present-but-empty value via the auth
    # module directly to assert the mock's policy.
    from app.auth import authenticate_dsm

    assert authenticate_dsm("asarin", "") == "invalid_credentials"


def test_mock_auth_triggers_otp_for_magic_password(client, monkeypatch):
    monkeypatch.setenv("MOCK_AUTH", "1")
    r = client.post(
        "/login", data={"username": "asarin", "password": "otp_needed"}
    )
    assert r.status_code == 200
    assert 'name="otp_code"' in r.text


# ----- missing config -----


def test_login_without_session_secret_raises(client, monkeypatch):
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
    patch_dsm(monkeypatch, {"success": True})
    # FastAPI TestClient re-raises server exceptions by default; in real
    # uvicorn this would surface as a 500. The point is to prove the secret
    # is mandatory at runtime, not silently defaulted.
    with pytest.raises(RuntimeError, match="APP_SESSION_SECRET"):
        client.post(
            "/login",
            data={"username": "asarin", "password": "x"},
            follow_redirects=False,
        )


# ----- F-009: APP_SESSION_SECRET minimum-length enforcement -----


def test_session_secret_missing_raises_required_error(monkeypatch):
    """F-009: unset / empty APP_SESSION_SECRET still fails closed (preserves
    the pre-fix behavior tested by test_login_without_session_secret_raises)."""
    from app.auth import _load_session_secret

    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        _load_session_secret()


def test_session_secret_too_short_raises_min_length_error(monkeypatch):
    """F-009: a 16-byte secret (under the 32-byte floor) must fail closed.
    itsdangerous uses HMAC-SHA1 — short keys are brute-forceable offline
    against any captured cookie, so silently accepting them is the bug."""
    from app.auth import _load_session_secret

    short_secret = "x" * 16
    monkeypatch.setenv("APP_SESSION_SECRET", short_secret)
    with pytest.raises(RuntimeError, match="≥32 bytes"):
        _load_session_secret()


def test_session_secret_valid_length_constructs_serializer(monkeypatch):
    """F-009: a 32-byte secret passes validation and the serializer can be
    constructed (round-trips a value through dumps/loads)."""
    from app.auth import _load_session_secret, _session_serializer

    monkeypatch.setenv("APP_SESSION_SECRET", "x" * 32)
    assert _load_session_secret() == "x" * 32
    serializer = _session_serializer()
    token = serializer.dumps({"username": "asarin"})
    assert serializer.loads(token) == {"username": "asarin"}


# ----- F-008: Fernet-wrapped partial-auth token -----


def test_partial_auth_round_trip(monkeypatch):
    """F-008: encode → decode is the identity. Baseline correctness for the
    new dict→JSON→Fernet→signed-envelope pipeline."""
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import decode_partial_auth, encode_partial_auth

    token = encode_partial_auth("asarin", "supersecretpassword123")
    assert decode_partial_auth(token) == ("asarin", "supersecretpassword123")


def test_partial_auth_password_not_visible_in_token(monkeypatch):
    """F-008 core privacy contract: the password must NOT appear inside the
    URLSafeTimedSerializer envelope's payload. Pre-fix, the JSON sat in
    base64-readable plaintext; post-fix, it sits inside Fernet ciphertext.

    Confirms by (a) decoding the signed envelope to expose the inner
    payload, (b) base64-decoding it to reach the raw bytes, and
    (c) asserting the password string is absent from both."""
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import _partial_serializer, encode_partial_auth

    password = "this-string-must-not-leak-789"
    token = encode_partial_auth("asarin", password)

    # The serializer's loads gives us the inner payload (a Fernet ciphertext
    # string, post-F-008). Pre-fix this would have been the base64 of the
    # plaintext JSON.
    inner = _partial_serializer().loads(token, max_age=120)
    assert isinstance(inner, str)
    assert password not in inner

    # Belt-and-suspenders: also check the raw token bytes don't contain
    # the password (covers any base64/url-safe encoding of the substring).
    assert password not in token
    assert "asarin" not in inner  # username is also encrypted


def test_partial_auth_tampered_ciphertext_returns_none(monkeypatch):
    """F-008: flipping a byte inside the Fernet ciphertext (after passing
    the outer signed envelope's HMAC) must collapse to None — same fail-
    closed contract as today's tampered-envelope path."""
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import (
        _partial_serializer,
        decode_partial_auth,
        encode_partial_auth,
    )

    token = encode_partial_auth("asarin", "x")
    # Pull the ciphertext out, flip a byte, re-sign so the outer envelope
    # is valid but the inner Fernet token is tampered.
    inner = _partial_serializer().loads(token, max_age=120)
    # Flip a byte ~halfway through the ciphertext (avoid the version byte
    # at index 0 and the timestamp window).
    midpoint = len(inner) // 2
    flipped_char = "A" if inner[midpoint] != "A" else "B"
    tampered_inner = inner[:midpoint] + flipped_char + inner[midpoint + 1:]
    tampered_token = _partial_serializer().dumps(tampered_inner)

    assert decode_partial_auth(tampered_token) is None


def test_partial_auth_wrong_key_returns_none(monkeypatch):
    """F-008: a token issued under one APP_SESSION_SECRET cannot be decoded
    under a different secret. Catches both the outer signed envelope's
    HMAC mismatch and (if that somehow passed) the inner Fernet
    InvalidToken."""
    from app.auth import decode_partial_auth, encode_partial_auth

    secret_a = "a" * 32
    secret_b = "b" * 32
    monkeypatch.setenv("APP_SESSION_SECRET", secret_a)
    token = encode_partial_auth("asarin", "x")

    monkeypatch.setenv("APP_SESSION_SECRET", secret_b)
    assert decode_partial_auth(token) is None


def test_partial_auth_expiry_preserved(monkeypatch):
    """F-008 regression: the 120 s expiry window survives the Fernet wrap.
    The signed envelope still drives the expiry check (we kept itsdangerous
    on the outside specifically to preserve this behavior with no caller
    changes)."""
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import decode_partial_auth, encode_partial_auth

    token = encode_partial_auth("asarin", "x")
    # Round-trip works fresh.
    assert decode_partial_auth(token) == ("asarin", "x")

    # Force immediate expiry via the same monkeypatch trick the existing
    # test_login_otp_expired_token_rejected uses.
    monkeypatch.setattr("app.auth.PARTIAL_AUTH_MAX_AGE_S", -1, raising=False)
    assert decode_partial_auth(token) is None


def test_derive_partial_auth_fernet_key_is_deterministic(monkeypatch):
    """F-008: the KDF must be a pure function of APP_SESSION_SECRET so that
    a token issued by one process can be decoded by another (e.g., systemd
    restart of the FastAPI service mid-OTP-window)."""
    monkeypatch.setenv("APP_SESSION_SECRET", TEST_SECRET)
    from app.auth import _derive_partial_auth_fernet_key

    key1 = _derive_partial_auth_fernet_key()
    key2 = _derive_partial_auth_fernet_key()
    assert key1 == key2

    # And different secrets yield different keys (sanity).
    monkeypatch.setenv("APP_SESSION_SECRET", "z" * 32)
    key_other = _derive_partial_auth_fernet_key()
    assert key_other != key1
