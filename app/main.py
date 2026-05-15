"""FastAPI application — login, role-based routing, scope-violation handler.

Routes:

    GET  /healthz                role-agnostic, no auth
    GET  /login                  role-agnostic
    POST /login                  role-agnostic
    POST /login/otp              role-agnostic (partial-auth token gate)
    GET  /logout                 role-agnostic
    GET  /                       redirects to /app or /admin by role
    GET  /app                    surgeon-only (placeholder; Spec C mounts Gradio)
    GET  /admin                  admin-only (placeholder; Spec C mounts Gradio)

Role enforcement lives at the prefix-level via ``Depends(require_role(...))``
on the surgeon / admin routers. Mismatch raises ``ScopeViolationError`` which
the central handler converts into a generic 403 + one ``scope_violation_log``
insert (same path as a SurgeonScope targeted method raising the same error
deep inside the call stack).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import (
    DSM_INVALID,
    DSM_NEEDS_OTP,
    DSM_SUCCESS,
    SESSION_COOKIE_NAME,
    authenticate_dsm,
    clear_session_cookie,
    current_user,
    current_user_required,
    decode_partial_auth,
    encode_partial_auth,
    lookup_active_user,
    set_session_cookie,
    username_from_request,
)
from app.db.connection import connect, utcnow
from app.exceptions import ScopeViolationError
from app.scopes import AdminScope, SurgeonScope, UserScope


# ----- HTML helpers (intentionally minimal — no template engine) -----


_LOGIN_FORM_HTML = """<!doctype html>
<html><head><title>Sign in</title></head>
<body>
<h1>Sign in</h1>
{error_block}
<form method="post" action="/login">
  <p><label>Username <input name="username" autofocus required></label></p>
  <p><label>Password <input name="password" type="password" required></label></p>
  <p><button type="submit">Sign in</button></p>
</form>
</body></html>
"""

_OTP_FORM_HTML = """<!doctype html>
<html><head><title>Two-factor code</title></head>
<body>
<h1>Two-factor code</h1>
{error_block}
<form method="post" action="/login/otp">
  <input type="hidden" name="partial_auth_token" value="{token}">
  <p><label>Code <input name="otp_code" inputmode="numeric" autofocus required></label></p>
  <p><button type="submit">Verify</button></p>
</form>
</body></html>
"""


def _render_login(error: str | None = None) -> str:
    block = f"<p style='color:#b00'>{error}</p>" if error else ""
    return _LOGIN_FORM_HTML.format(error_block=block)


def _render_otp(token: str, error: str | None = None) -> str:
    block = f"<p style='color:#b00'>{error}</p>" if error else ""
    return _OTP_FORM_HTML.format(token=token, error_block=block)


_GENERIC_LOGIN_ERROR = "Invalid credentials."
_GENERIC_FORBIDDEN_BODY = "<h1>Forbidden</h1>"


# ----- scope_violation_log writer -----


def _log_violation(
    username: str,
    resource: str,
    action: str,
    scope_at_time: str,
    user_agent: str | None,
) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO scope_violation_log "
            "(username, attempted_resource, attempted_action, scope_at_time, "
            " user_agent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, resource, action, scope_at_time, user_agent, utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


# ----- role-prefix dependency -----


def require_role(expected_role: str):
    """Return a FastAPI dependency that enforces ``user.role == expected_role``.
    On mismatch, raises ``ScopeViolationError`` (caught by the central handler
    which writes one violation-log row and returns a generic 403)."""

    def dep(
        request: Request, user: dict = Depends(current_user_required)
    ) -> dict:
        if user["role"] != expected_role:
            scope_tag = user["role"]
            if user["role"] == "surgeon" and user.get("folder_slug"):
                scope_tag = f"surgeon:{user['folder_slug']}"
            raise ScopeViolationError(
                resource=request.url.path,
                action=request.method,
                scope_at_time=scope_tag,
            )
        return user

    return dep


def build_scope(user: dict = Depends(current_user_required)) -> UserScope:
    if user["role"] == "admin":
        return AdminScope(user["username"])
    return SurgeonScope(user["username"], user["folder_slug"])


# ----- app + handlers -----


app = FastAPI(title="surgical-cv")


@app.exception_handler(ScopeViolationError)
async def scope_violation_handler(
    request: Request, exc: ScopeViolationError
) -> Response:
    username = username_from_request(request)
    if username is not None:
        _log_violation(
            username=username,
            resource=exc.resource,
            action=exc.action,
            scope_at_time=exc.scope_at_time,
            user_agent=request.headers.get("user-agent"),
        )
    return HTMLResponse(_GENERIC_FORBIDDEN_BODY, status_code=403)


# ----- public routes -----


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_form():
    return HTMLResponse(_render_login())


@app.post("/login")
async def login_submit(
    username: str = Form(...), password: str = Form(...)
) -> Response:
    result = authenticate_dsm(username, password)
    if result == DSM_NEEDS_OTP:
        token = encode_partial_auth(username, password)
        return HTMLResponse(_render_otp(token))
    if result != DSM_SUCCESS:
        return HTMLResponse(
            _render_login(error=_GENERIC_LOGIN_ERROR), status_code=401
        )

    user = lookup_active_user(username)
    if user is None:
        # Fail-closed: same generic error path as a real DSM rejection.
        return HTMLResponse(
            _render_login(error=_GENERIC_LOGIN_ERROR), status_code=401
        )

    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, username)
    return resp


@app.post("/login/otp")
async def login_otp_submit(
    partial_auth_token: str = Form(...),
    otp_code: str = Form(...),
) -> Response:
    decoded = decode_partial_auth(partial_auth_token)
    if decoded is None:
        return HTMLResponse(
            _render_login(error=_GENERIC_LOGIN_ERROR), status_code=401
        )
    username, password = decoded

    result = authenticate_dsm(username, password, otp_code=otp_code)
    if result != DSM_SUCCESS:
        return HTMLResponse(
            _render_login(error=_GENERIC_LOGIN_ERROR), status_code=401
        )

    user = lookup_active_user(username)
    if user is None:
        return HTMLResponse(
            _render_login(error=_GENERIC_LOGIN_ERROR), status_code=401
        )

    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, username)
    return resp


@app.get("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    clear_session_cookie(resp)
    return resp


@app.get("/")
async def root(user: dict | None = Depends(current_user)) -> Response:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    target = "/admin" if user["role"] == "admin" else "/app"
    return RedirectResponse(target, status_code=303)


# ----- role-prefix routers -----


surgeon_router = APIRouter(
    prefix="/app", dependencies=[Depends(require_role("surgeon"))]
)


@surgeon_router.get("", response_class=HTMLResponse)
async def surgeon_home(scope: UserScope = Depends(build_scope)) -> str:
    # Placeholder — Spec C will mount the surgeon Gradio Blocks at this prefix.
    return (
        "<!doctype html><html><body>"
        f"<h1>Surgeon Home</h1><p>Welcome, {scope.username}.</p>"
        "</body></html>"
    )


admin_router = APIRouter(
    prefix="/admin", dependencies=[Depends(require_role("admin"))]
)


@admin_router.get("", response_class=HTMLResponse)
async def admin_home(scope: UserScope = Depends(build_scope)) -> str:
    # Placeholder — Spec C will mount the admin Gradio Blocks at this prefix.
    return (
        "<!doctype html><html><body>"
        f"<h1>Admin Home</h1><p>Welcome, {scope.username}.</p>"
        "</body></html>"
    )


app.include_router(surgeon_router)
app.include_router(admin_router)
