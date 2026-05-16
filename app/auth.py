"""DSM WebAPI client, signed-cookie session, current-user dependency.

Auth flow:

    POST /login (username, password)
        -> authenticate_dsm(username, password)
            "success"             -> set session cookie, redirect to /
            "needs_otp"           -> render OTP form with signed partial-auth
                                     token carrying (username, password) for
                                     up to 120s; user does NOT re-post password
            "invalid_credentials" -> generic error, no info leak

    POST /login/otp (partial_auth_token, otp_code)
        -> decode token -> (username, password)
        -> authenticate_dsm(username, password, otp_code=...)
            "success" -> set session cookie, redirect to /
            anything else -> generic error

After any DSM "success", look up the username in app.db users. If not present
or active=0, reject with the same generic invalid-credentials message — same
output shape as a real auth failure, no enumeration leak.

Session cookie:
    name:      "app_session"
    payload:   {"username": "<username>"}
    signer:    itsdangerous.URLSafeTimedSerializer with salt "session"
    expiry:    8 hours (28800 s) enforced by ``loads(max_age=...)``
    secret:    APP_SESSION_SECRET env var (mandatory)
    secure:    True unless APP_DEV_MODE=1

Partial-auth token (between password and OTP):
    payload:   {"u": username, "p": password}
    pipeline:  dict → JSON → Fernet-encrypt → URLSafeTimedSerializer.dumps
               (F-008: the inner JSON sits inside Fernet ciphertext so the
               password is not readable from a captured token; the signed
               envelope provides the 120 s expiry)
    salt:      "partial-auth"
    expiry:    120 s
    fernet key: SHA-256("partial-auth-fernet:" + APP_SESSION_SECRET),
                base64url-encoded; derived lazily per call

DSM endpoint URL: NAS_DSM_URL env var. ``verify=False`` is acceptable for the
private-network link (DSM self-signed cert); cert pinning is a hardening
followup.

MOCK_AUTH=1 bypasses DSM with a fixture matcher (for local uvicorn smokes;
tests use monkeypatched httpx instead).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from base64 import urlsafe_b64encode
from typing import Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Cookie, Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.db.connection import connect


_log = logging.getLogger(__name__)

# ----- constants -----

SESSION_COOKIE_NAME = "app_session"
SESSION_MAX_AGE_S = 8 * 3600
PARTIAL_AUTH_MAX_AGE_S = 120

# itsdangerous uses HMAC-SHA1; key entropy is the only thing standing between
# a captured cookie and offline forgery. primer.md documents ≥32 bytes as the
# operator-side requirement; this constant enforces it at startup so a
# misconfigured env var fails closed instead of silently weakening sessions.
_MIN_SESSION_SECRET_LEN = 32

# F-008: domain-separation prefix for the partial-auth Fernet key. Mixed into
# the SHA-256 input so the Fernet key derived for partial-auth never collides
# with any other key the same secret might derive in the future (session
# cookie HMAC stays separate by virtue of itsdangerous's own salting; this
# prefix keeps Fernet's slot equally exclusive).
_PARTIAL_AUTH_FERNET_DOMAIN = b"partial-auth-fernet:"

DSM_SUCCESS = "success"
DSM_NEEDS_OTP = "needs_otp"
DSM_INVALID = "invalid_credentials"

DSMResult = Literal["success", "needs_otp", "invalid_credentials"]

# DSM API path appended to NAS_DSM_URL. The env var is the base only
# (scheme + host + port) — operators only need to remember the host, not
# the API path. Modern Synology DSM 7 routes auth via /webapi/entry.cgi.
_DSM_API_PATH = "/webapi/entry.cgi"


# ----- signed-token helpers -----


def _load_session_secret() -> str:
    """Read and validate ``APP_SESSION_SECRET``. Fails closed on missing /
    empty / too-short values so a weak key never reaches the serializer."""
    secret = os.environ.get("APP_SESSION_SECRET")
    if not secret:
        raise RuntimeError("APP_SESSION_SECRET env var is required")
    if len(secret) < _MIN_SESSION_SECRET_LEN:
        raise RuntimeError(
            f"APP_SESSION_SECRET must be ≥{_MIN_SESSION_SECRET_LEN} bytes"
        )
    return secret


def _session_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_load_session_secret(), salt="session")


def _partial_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_load_session_secret(), salt="partial-auth")


def _derive_partial_auth_fernet_key() -> bytes:
    """F-008: Derive a Fernet key for the partial-auth payload from
    APP_SESSION_SECRET via SHA-256 with a domain-separation prefix.

    F-009 enforces ≥32-byte input entropy on the secret; SHA-256 over
    ``domain || secret`` is a defensible single-purpose KDF for this use
    without pulling ``cryptography.hazmat`` HKDF. The prefix
    (``_PARTIAL_AUTH_FERNET_DOMAIN``) keeps this slot exclusive — any other
    key derived from the same secret in the future must use a different
    domain string."""
    secret = _load_session_secret().encode()
    digest = hashlib.sha256(_PARTIAL_AUTH_FERNET_DOMAIN + secret).digest()
    return urlsafe_b64encode(digest)


def _partial_auth_fernet() -> Fernet:
    """Lazy Fernet construction so the crypto primitive isn't built at import
    time (lets tests monkeypatch ``APP_SESSION_SECRET`` before any auth
    surface is touched)."""
    return Fernet(_derive_partial_auth_fernet_key())


def encode_session(username: str) -> str:
    return _session_serializer().dumps({"username": username})


def decode_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _session_serializer().loads(token, max_age=SESSION_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or "username" not in data:
        return None
    return data["username"]


def encode_partial_auth(username: str, password: str) -> str:
    """F-008: payload pipeline is dict → JSON → Fernet-encrypt → signed envelope.

    The Fernet wrap means the password is not readable by anyone who captures
    the token from browser devtools, an HTTPS proxy log, or a Cloudflare
    tunnel intercept during the 120 s OTP window. The outer
    ``URLSafeTimedSerializer`` provides the signing layer + the existing
    expiry mechanism (Fernet has its own TTL but reusing the serializer's
    expiry keeps the change minimal-surface — see spec)."""
    payload = json.dumps({"u": username, "p": password}).encode()
    ciphertext = _partial_auth_fernet().encrypt(payload).decode()
    return _partial_serializer().dumps(ciphertext)


def decode_partial_auth(token: str | None) -> tuple[str, str] | None:
    """Reverse the encode pipeline: signed envelope → ciphertext → Fernet-
    decrypt → JSON → ``(u, p)``. Any failure at any layer (bad signature,
    expired envelope, tampered/wrong-key ciphertext, unparseable payload,
    shape mismatch) collapses to ``None`` — same fail-closed contract as the
    pre-F-008 implementation, so call sites in ``app/main.py`` don't change."""
    if not token:
        return None
    try:
        ciphertext = _partial_serializer().loads(
            token, max_age=PARTIAL_AUTH_MAX_AGE_S
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(ciphertext, str):
        return None
    try:
        payload = _partial_auth_fernet().decrypt(ciphertext.encode())
    except InvalidToken:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "u" not in data or "p" not in data:
        return None
    return data["u"], data["p"]


# ----- DSM client -----


def _dsm_endpoint(base: str) -> str:
    """Compose the DSM API endpoint URL from the operator-supplied base.
    Strips any trailing slashes so ``https://10.10.0.2:5001`` and
    ``https://10.10.0.2:5001/`` produce the same target."""
    return base.rstrip("/") + _DSM_API_PATH


def authenticate_dsm(
    username: str, password: str, otp_code: str | None = None
) -> DSMResult:
    """Call DSM WebAPI and reduce to one of the three locked return shapes.

    Failure paths emit narrow, sanitized WARNING logs (HTTP error type +
    status code, JSON-parse vs. URL, DSM error.code → mapped result).
    Logs never include username, password, otp_code, or cookie material —
    they exist for operational visibility into the failure mode, not for
    credential capture."""
    if os.environ.get("MOCK_AUTH") == "1":
        return _mock_dsm(username, password, otp_code)

    base = os.environ.get("NAS_DSM_URL")
    if not base:
        # Misconfigured deployment — fail closed.
        _log.warning("DSM auth: NAS_DSM_URL not set; failing closed")
        return DSM_INVALID
    url = _dsm_endpoint(base)

    params = {
        "api": "SYNO.API.Auth",
        "version": "3",
        "method": "login",
        "account": username,
        "passwd": password,
        "format": "cookie",
    }
    if otp_code is not None:
        params["otp_code"] = otp_code

    try:
        # F-020: TLS verification env-gated. Default preserves the prior
        # behavior (verify=False — DSM uses a self-signed cert on the
        # private network); operators can flip on cert verification by
        # setting DSM_TLS_VERIFY=1 once cert pinning lands.
        verify = os.environ.get("DSM_TLS_VERIFY", "0") != "0"
        resp = httpx.post(url, data=params, verify=verify, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        _log.warning(
            "DSM auth: %s%s",
            type(e).__name__,
            f" status={status}" if status is not None else "",
        )
        return DSM_INVALID

    try:
        data = resp.json()
    except ValueError:
        _log.warning("DSM auth: non-JSON response from %s", url)
        return DSM_INVALID

    if data.get("success") is True:
        return DSM_SUCCESS

    err_code = (data.get("error") or {}).get("code")
    # DSM 7 error codes: 403 = 2FA required, 404 = 2FA code rejected,
    # 405 = enforce 2FA. Everything else is a generic invalid-credentials.
    # 403 has two operationally distinct meanings that look identical here:
    # (a) account has TOTP enabled, password correct, OTP code missing on
    # this attempt (resolution: prompt the user for their code); (b) DSM is
    # system-enforcing 2FA but the account has no TOTP seed paired yet
    # (resolution: admin pairs an authenticator before the user can log
    # in). Same status code, different ops fix — don't bisect them in
    # code, they're identical at the wire layer.
    if err_code in (403, 405) and otp_code is None:
        _log.warning("DSM auth: error.code=%s -> NEEDS_OTP", err_code)
        return DSM_NEEDS_OTP
    _log.warning("DSM auth: error.code=%s -> INVALID", err_code)
    return DSM_INVALID


def _mock_dsm(
    username: str, password: str, otp_code: str | None
) -> DSMResult:
    """Local-dev DSM stand-in. Treats password ``otp_needed`` as triggering
    the 2FA flow; any non-empty password otherwise succeeds; empty fails."""
    if not username or not password:
        return DSM_INVALID
    if password == "otp_needed":
        if otp_code is None:
            return DSM_NEEDS_OTP
        if otp_code != "123456":
            return DSM_INVALID
        return DSM_SUCCESS
    return DSM_SUCCESS


# ----- users lookup (fail-closed gate) -----


def lookup_active_user(username: str) -> dict | None:
    """Return the full ``users`` row (as a dict) for an ACTIVE user, or
    ``None`` if missing / inactive. Same return shape collapses both into
    the same caller-side reject path — no enumeration leak.

    Returning every column (username, role, folder_slug, specialty,
    display_name, email, active, created_at, last_login_at, notes) keeps
    downstream consumers — scope construction, identity rendering,
    picklist filtering — from re-doing the lookup."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username, role, folder_slug, specialty, display_name, "
            "       email, active, created_at, last_login_at, notes "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["active"] != 1:
        return None
    return dict(row)


# ----- cookie helpers -----


def is_dev_mode() -> bool:
    return os.environ.get("APP_DEV_MODE") == "1"


def set_session_cookie(response, username: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=encode_session(username),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        secure=not is_dev_mode(),
        samesite="lax",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=not is_dev_mode(),
        samesite="lax",
    )


# ----- FastAPI dependency: current user -----


def current_user(
    app_session: str | None = Cookie(default=None),
) -> dict | None:
    """Decode the session cookie and resolve to a live user row. Returns None
    if the cookie is missing / invalid / expired / the user is inactive."""
    username = decode_session(app_session)
    if username is None:
        return None
    return lookup_active_user(username)


def current_user_required(
    user: dict | None = Depends(current_user),
) -> dict:
    """Same as ``current_user`` but raises 401 on missing/invalid session."""
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def username_from_request(request: Request) -> str | None:
    """Decode the session cookie out of a raw Request — used by the exception
    handler to attribute a scope_violation_log row without re-injecting the
    full ``current_user`` dependency chain."""
    return decode_session(request.cookies.get(SESSION_COOKIE_NAME))


# ----- identity string for Gradio mounts -----


_GENERIC_IDENTITY = "Signed in."


def identity_string_for_request(request) -> str:
    """Render the "Signed in as ..." line for Gradio mounts.

    Reads ``app_session`` from the request cookies, decodes it, looks up the
    active user, and returns a formatted identity string. Any failure
    (missing/bad cookie, missing user, inactive user, ``None`` request)
    collapses to the generic fallback — the role-prefix auth already gated
    the request, so this function is informational only and must never crash
    the page render.
    """
    if request is None:
        return _GENERIC_IDENTITY
    cookies = getattr(request, "cookies", None) or {}
    username = decode_session(cookies.get(SESSION_COOKIE_NAME))
    if not username:
        return _GENERIC_IDENTITY
    user = lookup_active_user(username)
    if user is None:
        return _GENERIC_IDENTITY
    label = user.get("display_name") or user["username"]
    folder = user.get("folder_slug") or "admin"
    specialty = user.get("specialty") or "all specialties"
    return f"Signed in as {label} ({folder}, {specialty})"
