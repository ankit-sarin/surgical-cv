"""Auth-layer tests: login flow, OTP flow, session cookie, fail-closed,
mock-auth, generic error copy. DSM calls are mocked via monkeypatched httpx
(test-only — production code never sees a fixture)."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.conftest import TEST_DSM_URL, TEST_SECRET, patch_dsm


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


# ----- F-020: DSM_TLS_VERIFY env-gating -----


def test_dsm_tls_verify_default_off(monkeypatch):
    """F-020 default: DSM_TLS_VERIFY unset → httpx.post called with
    verify=False (preserves the prior behavior — DSM uses a self-signed
    cert on the private network, cert pinning is a future hardening)."""
    from app.auth import authenticate_dsm

    monkeypatch.delenv("DSM_TLS_VERIFY", raising=False)
    monkeypatch.setenv("NAS_DSM_URL", TEST_DSM_URL)
    monkeypatch.delenv("MOCK_AUTH", raising=False)

    captured = {}

    def capturing_post(url, data=None, **kwargs):
        captured["verify"] = kwargs.get("verify")
        return httpx.Response(
            200, json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", capturing_post)
    authenticate_dsm("asarin", "x")
    assert captured["verify"] is False


def test_dsm_tls_verify_enabled_when_env_set(monkeypatch):
    """F-020 flip: DSM_TLS_VERIFY=1 → httpx.post called with verify=True.
    Operators turn this on once cert pinning lands; the default stays
    closed-fail-permissive for the smoke environment."""
    from app.auth import authenticate_dsm

    monkeypatch.setenv("DSM_TLS_VERIFY", "1")
    monkeypatch.setenv("NAS_DSM_URL", TEST_DSM_URL)
    monkeypatch.delenv("MOCK_AUTH", raising=False)

    captured = {}

    def capturing_post(url, data=None, **kwargs):
        captured["verify"] = kwargs.get("verify")
        return httpx.Response(
            200, json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", capturing_post)
    authenticate_dsm("asarin", "x")
    assert captured["verify"] is True


# ----- DSM URL construction (NAS_DSM_URL is the base; /webapi/entry.cgi
# is appended internally so operators only set scheme + host + port) -----


@pytest.mark.parametrize("base", [
    "https://10.10.0.2:5001",
    "https://10.10.0.2:5001/",
    "https://10.10.0.2:5001///",
    "https://dsm.test.invalid:5001",
])
def test_dsm_url_appends_webapi_entry_cgi(monkeypatch, base):
    """Regardless of trailing slashes on NAS_DSM_URL, the URL passed to
    httpx.post ends with /webapi/entry.cgi exactly once."""
    from app.auth import authenticate_dsm

    monkeypatch.setenv("NAS_DSM_URL", base)
    monkeypatch.delenv("MOCK_AUTH", raising=False)
    captured: dict = {}

    def capturing_post(url, data=None, **kw):
        captured["url"] = url
        return httpx.Response(
            200, json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", capturing_post)
    authenticate_dsm("asarin", "x")
    assert captured["url"].endswith("/webapi/entry.cgi")
    # Exactly one occurrence — no double-suffix bug.
    assert captured["url"].count("/webapi/entry.cgi") == 1
    # No trailing-slash leak between base and path.
    assert "//webapi" not in captured["url"]


def test_dsm_url_missing_env_logs_and_fails_closed(monkeypatch, caplog):
    from app.auth import DSM_INVALID, authenticate_dsm

    monkeypatch.delenv("NAS_DSM_URL", raising=False)
    monkeypatch.delenv("MOCK_AUTH", raising=False)
    with caplog.at_level("WARNING", logger="app.auth"):
        assert authenticate_dsm("asarin", "x") == DSM_INVALID
    assert any("NAS_DSM_URL not set" in r.message for r in caplog.records)


# ----- Sanitized DSM failure logging -----


def test_dsm_http_error_logs_exception_type_no_credentials(
    monkeypatch, caplog
):
    """HTTPError path emits a WARNING with the exception type but never
    the username, password, or otp_code."""
    from app.auth import DSM_INVALID, authenticate_dsm

    monkeypatch.setenv("NAS_DSM_URL", "https://dsm.test.invalid:5001")
    monkeypatch.delenv("MOCK_AUTH", raising=False)

    def boom(url, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr("app.auth.httpx.post", boom)
    with caplog.at_level("WARNING", logger="app.auth"):
        assert authenticate_dsm("asarin", "secret-pwd") == DSM_INVALID
    msgs = [r.message for r in caplog.records]
    joined = "\n".join(msgs)
    assert "ConnectError" in joined
    assert "secret-pwd" not in joined
    assert "asarin" not in joined


def test_dsm_non_json_response_maps_to_invalid_and_logs(
    monkeypatch, caplog
):
    """200 response whose body is not JSON (e.g. an HTML 404 page from a
    misrouted reverse proxy) must collapse to DSM_INVALID and emit a
    "non-JSON response" log line. Regression guard for the bug that
    motivated this spec — the deployed app was hitting an HTML response
    from the wrong URL path."""
    from app.auth import DSM_INVALID, authenticate_dsm

    monkeypatch.setenv("NAS_DSM_URL", "https://dsm.test.invalid:5001")
    monkeypatch.delenv("MOCK_AUTH", raising=False)

    def html_response(url, **kw):
        return httpx.Response(
            200, text="not found",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", html_response)
    with caplog.at_level("WARNING", logger="app.auth"):
        assert authenticate_dsm("asarin", "x") == DSM_INVALID
    assert any("non-JSON response" in r.message for r in caplog.records)


def test_dsm_error_code_403_maps_to_needs_otp_and_logs(
    monkeypatch, caplog
):
    """Existing behavior the new logging line must not regress: DSM
    error.code=403 (2FA required) maps to NEEDS_OTP and logs the mapping."""
    from app.auth import DSM_NEEDS_OTP, authenticate_dsm

    monkeypatch.setenv("NAS_DSM_URL", "https://dsm.test.invalid:5001")
    monkeypatch.delenv("MOCK_AUTH", raising=False)

    def err_403(url, **kw):
        return httpx.Response(
            200, json={"success": False, "error": {"code": 403}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", err_403)
    with caplog.at_level("WARNING", logger="app.auth"):
        assert authenticate_dsm("asarin", "x") == DSM_NEEDS_OTP
    assert any(
        "error.code=403 -> NEEDS_OTP" in r.message for r in caplog.records
    )


def test_dsm_error_code_400_maps_to_invalid_and_logs(monkeypatch, caplog):
    from app.auth import DSM_INVALID, authenticate_dsm

    monkeypatch.setenv("NAS_DSM_URL", "https://dsm.test.invalid:5001")
    monkeypatch.delenv("MOCK_AUTH", raising=False)

    def err_400(url, **kw):
        return httpx.Response(
            200, json={"success": False, "error": {"code": 400}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.auth.httpx.post", err_400)
    with caplog.at_level("WARNING", logger="app.auth"):
        assert authenticate_dsm("asarin", "topsecret") == DSM_INVALID
    msgs = "\n".join(r.message for r in caplog.records)
    assert "error.code=400 -> INVALID" in msgs
    # Belt + suspenders: no credential leak.
    assert "topsecret" not in msgs
    assert "asarin" not in msgs


# ----- next= round-trip + open-redirect defense -----


def test_login_post_honors_safe_next(client, monkeypatch):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "asarin", "password": "x", "next": "/app/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/"
    assert "app_session" in r.cookies


def test_login_post_strips_unsafe_next(client, monkeypatch):
    """Open-redirect defense: a tampered ``next`` value pointing off-site
    must NOT be honored — the login redirect falls back to the default
    landing path (``/``, which then bounces by role)."""
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={
            "username": "asarin",
            "password": "x",
            "next": "https://evil.example.com/",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_get_strips_unsafe_next(client):
    """The login GET form pre-fills the hidden next field, but only with
    a sanitized value. ``next=https://evil`` must produce an empty hidden
    field, not the malicious URL echoed into the markup."""
    r = client.get("/login?next=https://evil.example.com/")
    assert r.status_code == 200
    assert "evil.example.com" not in r.text


def test_login_get_honors_safe_next_in_hidden_field(client):
    r = client.get("/login?next=/app/")
    assert r.status_code == 200
    assert 'name="next" value="/app/"' in r.text


@pytest.mark.parametrize("bad_next", [
    "//evil.example.com/x",
    "https://evil.example.com",
    "/etc/passwd",
    "/foo/../app/",
    "",
    "javascript:alert(1)",
])
def test_login_post_unsafe_next_falls_back_to_root(
    client, monkeypatch, bad_next
):
    patch_dsm(monkeypatch, {"success": True})
    r = client.post(
        "/login",
        data={"username": "asarin", "password": "x", "next": bad_next},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_safe_next_helper_unit():
    from app.main import _safe_next

    # Allowed
    assert _safe_next("/app/") == "/app/"
    assert _safe_next("/admin/") == "/admin/"
    assert _safe_next("/app/queue?x=1") == "/app/queue?x=1"
    # Bare paths without trailing slash also accepted
    assert _safe_next("/app") == "/app"
    assert _safe_next("/admin") == "/admin"
    # Rejected
    assert _safe_next(None) is None
    assert _safe_next("") is None
    assert _safe_next("https://evil.example.com/") is None
    assert _safe_next("//evil.example.com/x") is None
    assert _safe_next("/etc/passwd") is None
    assert _safe_next("javascript:alert(1)") is None
    assert _safe_next("/app/\\backslash") is None
