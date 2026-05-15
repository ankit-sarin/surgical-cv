"""FastAPI application — login, role-based routing, scope-violation handler,
Gradio mounts at /app (surgeon) and /admin (admin).

Routes:

    GET  /healthz                role-agnostic, no auth
    GET  /login                  role-agnostic
    POST /login                  role-agnostic
    POST /login/otp              role-agnostic (partial-auth token gate)
    GET  /logout                 role-agnostic
    GET  /                       redirects to /app or /admin by role
    /app/*                       surgeon-only Gradio mount
    /admin/*                     admin-only Gradio mount

Role enforcement for the Gradio mounts lives in ``_gradio_auth_dep(role)``,
passed to ``gr.mount_gradio_app(auth_dependency=...)``. Mismatch → inline
violation-log write + ``HTTPException(403)``. We log + raise inline rather
than letting the central ``ScopeViolationError`` handler do it because Gradio
mounts as a separate FastAPI sub-app whose exception handlers don't share the
parent's registry — the central handler still catches direct
``ScopeViolationError``s from non-Gradio code paths (e.g.
``SurgeonScope.read_case`` reached via ``build_scope``).
"""

from __future__ import annotations

import gradio as gr
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.admin_app import build_admin_app
from app.auth import (
    DSM_NEEDS_OTP,
    DSM_SUCCESS,
    SESSION_COOKIE_NAME,
    authenticate_dsm,
    clear_session_cookie,
    current_user,
    current_user_required,
    decode_partial_auth,
    decode_session,
    encode_partial_auth,
    lookup_active_user,
    set_session_cookie,
    username_from_request,
)
from app.db.connection import connect, utcnow
from app.exceptions import ScopeViolationError
from app.repos import (
    CsvCaseRepository,
    CsvPipelineStateRepository,
    FilesystemRawSegmentRepository,
    Repos,
    SqliteAttentionItemsRepository,
    SqlitePicklistRepository,
)
from app.scopes import AdminScope, SurgeonScope, UserScope
from app.surgeon_app import build_surgeon_app


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


def _scope_tag_for(user: dict) -> str:
    if user["role"] == "admin":
        return "admin"
    return f"surgeon:{user.get('folder_slug') or ''}"


# ----- scope dependency (used by non-Gradio routes / test integration) -----


def build_scope(
    user: dict = Depends(current_user_required),
) -> UserScope:
    repos = Repos(
        case=CsvCaseRepository(),
        segment=FilesystemRawSegmentRepository(),
        picklist=SqlitePicklistRepository(),
        pipeline_state=CsvPipelineStateRepository(),
        attention=SqliteAttentionItemsRepository(),
    )
    if user["role"] == "admin":
        return AdminScope(user["username"], repos)
    return SurgeonScope(
        user["username"],
        user["folder_slug"],
        repos,
        specialty=user.get("specialty"),
    )


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


# ----- Gradio mounts (role-enforced via auth_dependency) -----


def _gradio_auth_dep(expected_role: str):
    """Return a callable suitable for ``mount_gradio_app(auth_dependency=...)``.

    On success returns the username (Gradio stashes it on request state). On
    missing / invalid / inactive session: raises 401. On role mismatch: writes
    one ``scope_violation_log`` row inline and raises 403. Inline logging
    rather than via the central ``ScopeViolationError`` handler because the
    Gradio mount is a sub-app whose exception-handler registry is independent
    of the parent app's.
    """

    def dep(request: Request) -> str:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        username = decode_session(cookie)
        if not username:
            raise HTTPException(status_code=401, detail="authentication required")
        user = lookup_active_user(username)
        if user is None:
            raise HTTPException(status_code=401, detail="authentication required")
        if user["role"] != expected_role:
            _log_violation(
                username=username,
                resource=request.url.path,
                action=request.method,
                scope_at_time=_scope_tag_for(user),
                user_agent=request.headers.get("user-agent"),
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        return username

    return dep


gr.mount_gradio_app(
    app,
    build_surgeon_app(),
    path="/app",
    auth_dependency=_gradio_auth_dep("surgeon"),
)
gr.mount_gradio_app(
    app,
    build_admin_app(),
    path="/admin",
    auth_dependency=_gradio_auth_dep("admin"),
)
