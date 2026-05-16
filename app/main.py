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

from urllib.parse import quote

import gradio as gr
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

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
    CsvCaseManifestRepository,
    CsvCaseRepository,
    CsvPipelineStateRepository,
    FilesystemRawSegmentRepository,
    Repos,
    SqliteAttentionItemsRepository,
    SqlitePicklistRepository,
)
from app.admin_app import ADMIN_CSS, ADMIN_THEME, build_admin_app
from app.scopes import AdminScope, SurgeonScope, UserScope
from app.surgeon_app import SURGEON_CSS, SURGEON_THEME, build_surgeon_app


# ----- HTML helpers (intentionally minimal — no template engine) -----


_LOGIN_FORM_HTML = """<!doctype html>
<html><head><title>Sign in</title></head>
<body>
<h1>Sign in</h1>
{error_block}
<form method="post" action="/login">
  <input type="hidden" name="next" value="{next_value}">
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
  <input type="hidden" name="next" value="{next_value}">
  <p><label>Code <input name="otp_code" inputmode="numeric" autofocus required></label></p>
  <p><button type="submit">Verify</button></p>
</form>
</body></html>
"""

# HTML attribute escaping for the next= round-trip. quote(safe="/") keeps
# the value URL-safe; html.escape stops a tampered cookie from breaking
# out of the attribute (defense in depth — _safe_next already rejects
# anything outside /app/ /admin/, so the attacker would need a
# pre-validated payload to even reach this template).
from html import escape as _html_escape


def _render_login(error: str | None = None, next_value: str = "") -> str:
    block = f"<p style='color:#b00'>{error}</p>" if error else ""
    return _LOGIN_FORM_HTML.format(
        error_block=block,
        next_value=_html_escape(next_value, quote=True),
    )


def _render_otp(token: str, error: str | None = None, next_value: str = "") -> str:
    block = f"<p style='color:#b00'>{error}</p>" if error else ""
    return _OTP_FORM_HTML.format(
        token=token,
        error_block=block,
        next_value=_html_escape(next_value, quote=True),
    )


_GENERIC_LOGIN_ERROR = "Invalid credentials."
_GENERIC_FORBIDDEN_BODY = "<h1>Forbidden</h1>"


# ----- next= validation (open-redirect defense) -----


def _safe_next(next_param: str | None) -> str | None:
    """Return the ``next`` value iff it's a relative path under /app/ or
    /admin/. Anything else (absolute URL, scheme-relative ``//``, path
    outside the Gradio mounts, ``..``-traversal) collapses to ``None`` so
    the caller falls back to the default redirect target.

    Open-redirect defense: a malicious link like ``/login?next=https://
    evil.example.com`` must NOT bounce the user off-site after a
    successful login."""
    if not next_param:
        return None
    if not next_param.startswith("/"):
        return None
    if next_param.startswith("//"):
        # Protocol-relative URL — browsers treat ``//evil.com/x`` as
        # ``https://evil.com/x``. Reject.
        return None
    if "\\" in next_param or "\x00" in next_param:
        return None
    # Allow only the two Gradio mount prefixes (and their bare forms).
    allowed = ("/app/", "/admin/")
    if next_param in ("/app", "/admin"):
        return next_param
    if any(next_param.startswith(p) for p in allowed):
        return next_param
    return None


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
        case_manifest=CsvCaseManifestRepository(),
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
async def login_form(next: str | None = Query(default=None)):
    safe = _safe_next(next) or ""
    return HTMLResponse(_render_login(next_value=safe))


@app.post("/login")
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
) -> Response:
    safe_next = _safe_next(next)
    result = authenticate_dsm(username, password)
    if result == DSM_NEEDS_OTP:
        token = encode_partial_auth(username, password)
        return HTMLResponse(_render_otp(token, next_value=safe_next or ""))
    if result != DSM_SUCCESS:
        return HTMLResponse(
            _render_login(
                error=_GENERIC_LOGIN_ERROR, next_value=safe_next or "",
            ),
            status_code=401,
        )

    user = lookup_active_user(username)
    if user is None:
        return HTMLResponse(
            _render_login(
                error=_GENERIC_LOGIN_ERROR, next_value=safe_next or "",
            ),
            status_code=401,
        )

    resp = RedirectResponse(safe_next or "/", status_code=303)
    set_session_cookie(resp, username)
    return resp


@app.post("/login/otp")
async def login_otp_submit(
    partial_auth_token: str = Form(...),
    otp_code: str = Form(...),
    next: str = Form(default=""),
) -> Response:
    safe_next = _safe_next(next)
    decoded = decode_partial_auth(partial_auth_token)
    if decoded is None:
        return HTMLResponse(
            _render_login(
                error=_GENERIC_LOGIN_ERROR, next_value=safe_next or "",
            ),
            status_code=401,
        )
    username, password = decoded

    result = authenticate_dsm(username, password, otp_code=otp_code)
    if result != DSM_SUCCESS:
        return HTMLResponse(
            _render_login(
                error=_GENERIC_LOGIN_ERROR, next_value=safe_next or "",
            ),
            status_code=401,
        )

    user = lookup_active_user(username)
    if user is None:
        return HTMLResponse(
            _render_login(
                error=_GENERIC_LOGIN_ERROR, next_value=safe_next or "",
            ),
            status_code=401,
        )

    resp = RedirectResponse(safe_next or "/", status_code=303)
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


def _login_redirect_for(request: Request) -> HTTPException:
    """Build a 303 redirect to ``/login?next=<original-path>`` for an
    unauthenticated browser hit on a Gradio mount. Raised as an
    HTTPException so it propagates through the same path Gradio's
    auth_dependency uses for 401/403; FastAPI's default exception handler
    honors the Location header on the response."""
    target_path = request.url.path
    next_param = quote(target_path, safe="")
    return HTTPException(
        status_code=303,
        detail="login required",
        headers={"Location": f"/login?next={next_param}"},
    )


def _gradio_auth_dep(expected_role: str):
    """Return a callable suitable for ``mount_gradio_app(auth_dependency=...)``.

    On success returns the username (Gradio stashes it on request state). On
    missing / invalid / inactive session: raises HTTPException(303) with a
    Location header pointing at ``/login?next=<original-path>`` so a cold
    browser visit lands on the login form rather than a JSON 401 the user
    can't act on. On role mismatch: writes one ``scope_violation_log`` row
    inline and raises 403. Inline logging rather than via the central
    ``ScopeViolationError`` handler because the Gradio mount is a sub-app
    whose exception-handler registry is independent of the parent app's.
    """

    def dep(request: Request) -> str:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        username = decode_session(cookie)
        if not username:
            raise _login_redirect_for(request)
        user = lookup_active_user(username)
        if user is None:
            raise _login_redirect_for(request)
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
    theme=SURGEON_THEME,
    css=SURGEON_CSS,
)
gr.mount_gradio_app(
    app,
    build_admin_app(),
    path="/admin",
    auth_dependency=_gradio_auth_dep("admin"),
    theme=ADMIN_THEME,
    css=ADMIN_CSS,
)
