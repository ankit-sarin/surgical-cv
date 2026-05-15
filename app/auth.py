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
    salt:      "partial-auth"
    expiry:    120 s

DSM endpoint URL: NAS_DSM_URL env var. ``verify=False`` is acceptable for the
private-network link (DSM self-signed cert); cert pinning is a hardening
followup.

MOCK_AUTH=1 bypasses DSM with a fixture matcher (for local uvicorn smokes;
tests use monkeypatched httpx instead).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Literal

import httpx
from fastapi import Cookie, Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.db.connection import connect

# ----- constants -----

SESSION_COOKIE_NAME = "app_session"
SESSION_MAX_AGE_S = 8 * 3600
PARTIAL_AUTH_MAX_AGE_S = 120

DSM_SUCCESS = "success"
DSM_NEEDS_OTP = "needs_otp"
DSM_INVALID = "invalid_credentials"

DSMResult = Literal["success", "needs_otp", "invalid_credentials"]


# ----- signed-token helpers -----


def _session_serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("APP_SESSION_SECRET")
    if not secret:
        raise RuntimeError("APP_SESSION_SECRET env var is required")
    return URLSafeTimedSerializer(secret, salt="session")


def _partial_serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("APP_SESSION_SECRET")
    if not secret:
        raise RuntimeError("APP_SESSION_SECRET env var is required")
    return URLSafeTimedSerializer(secret, salt="partial-auth")


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
    return _partial_serializer().dumps({"u": username, "p": password})


def decode_partial_auth(token: str | None) -> tuple[str, str] | None:
    if not token:
        return None
    try:
        data = _partial_serializer().loads(token, max_age=PARTIAL_AUTH_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or "u" not in data or "p" not in data:
        return None
    return data["u"], data["p"]


# ----- DSM client -----


def authenticate_dsm(
    username: str, password: str, otp_code: str | None = None
) -> DSMResult:
    """Call DSM WebAPI and reduce to one of the three locked return shapes."""
    if os.environ.get("MOCK_AUTH") == "1":
        return _mock_dsm(username, password, otp_code)

    url = os.environ.get("NAS_DSM_URL")
    if not url:
        # Misconfigured deployment — fail closed.
        return DSM_INVALID

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
        resp = httpx.post(url, data=params, verify=False, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return DSM_INVALID

    if data.get("success") is True:
        return DSM_SUCCESS

    err_code = (data.get("error") or {}).get("code")
    # DSM 7 error codes: 403 = 2FA required, 404 = 2FA code rejected,
    # 405 = enforce 2FA. Everything else is a generic invalid-credentials.
    if err_code in (403, 405) and otp_code is None:
        return DSM_NEEDS_OTP
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
    """Return {username, role, folder_slug, specialty} for an ACTIVE user, or
    ``None`` if missing / inactive. Same return shape collapses both into the
    same caller-side reject path — no enumeration leak."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username, role, folder_slug, specialty, active "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["active"] != 1:
        return None
    return {
        "username": row["username"],
        "role": row["role"],
        "folder_slug": row["folder_slug"],
        "specialty": row["specialty"],
    }


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
