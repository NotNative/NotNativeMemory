"""
HTML routes for the web admin / user GUI.

Mounted on the same Starlette app as the MCP and /auth/* routes via
`register_routes(mcp)`. Uses BearerAuthMiddleware for auth, with a
cookie fallback so browsers don't have to send the Authorization
header on every page load.

First slice: login / register / logout, memory list + delete, token
management. Edit, rescope, bulk ops, and filters come in later slices.
"""

from __future__ import annotations

import os
from uuid import UUID

import asyncpg
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from lib import auth, auth_db
from lib.csrf import ensure_csrf, check_csrf


# -- Template loader --------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_TEMPLATE_DIR = os.path.join(_ROOT, "templates")

templates = Jinja2Templates(directory=_TEMPLATE_DIR)


# -- Session cookie ---------------------------------------------------------

SESSION_COOKIE_NAME = "nnm_session"


def _cookie_secure() -> bool:
    """
    Should session cookies carry the Secure attribute? On by default
    behind a real TLS-terminating proxy; off for local HTTP dev. The
    env var is read per-request so tests can flip it without a reload.
    """
    return os.environ.get("MEMORY_COOKIE_SECURE", "") in ("1", "true", "yes")


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# -- View helpers -----------------------------------------------------------


def _context_for(request: Request, **extra) -> dict:
    """Base template context. Adds the authenticated identity so nav renders.

    CSRF token is not populated here because the cookie is set on the
    response, which the view handler hasn't built yet. View handlers
    that render a form call `_render_with_csrf` instead.
    """
    ctx = {
        "request": request,
        "username": getattr(request.state, "username", None),
        "user_id": getattr(request.state, "user_id", None),
        "csrf_token": request.cookies.get("nnm_csrf", ""),
    }
    ctx.update(extra)
    return ctx


def _render_with_csrf(
    request: Request, template: str, status_code: int = 200, **extra,
):
    """
    Render a template with a guaranteed-present csrf_token in context.
    Mints the cookie onto the response if missing.
    """
    ctx = _context_for(request, **extra)
    response = templates.TemplateResponse(
        template, ctx, status_code=status_code,
    )
    token = ensure_csrf(request, response)
    # Jinja already rendered with whatever was in the cookie at read
    # time. If we just minted a fresh token, rewrite the rendered body
    # so the form carries the new value that the browser is about to
    # receive.
    if not ctx["csrf_token"]:
        ctx["csrf_token"] = token
        response = templates.TemplateResponse(
            template, ctx, status_code=status_code,
        )
        ensure_csrf(request, response)
    return response


def _require_login(request: Request) -> RedirectResponse | None:
    """Return a redirect response if not logged in, else None."""
    if not getattr(request.state, "user_id", None):
        return RedirectResponse("/login", status_code=302)
    return None


# -- Routes -----------------------------------------------------------------


def register_routes(mcp) -> None:
    """Attach all HTML / form routes to the FastMCP instance."""

    @mcp.custom_route("/", methods=["GET"])
    async def root(request: Request):
        if getattr(request.state, "user_id", None):
            return RedirectResponse("/memories", status_code=302)
        return RedirectResponse("/login", status_code=302)

    # -- Login ------------------------------------------------------------

    @mcp.custom_route("/login", methods=["GET"])
    async def login_page(request: Request):
        if getattr(request.state, "user_id", None):
            return RedirectResponse("/memories", status_code=302)
        return _render_with_csrf(request, "login.html")

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""

        record = await auth_db.get_user_by_username(username)
        if record is None or not auth.verify_secret(password, record["password_hash"]):
            return _render_with_csrf(
                request, "login.html",
                status_code=401,
                error="Invalid username or password.",
            )

        token = await auth_db.create_token(record["id"], label="web-session")
        resp = RedirectResponse("/memories", status_code=303)
        _set_session_cookie(resp, token["token"])
        return resp

    # -- Register ---------------------------------------------------------

    @mcp.custom_route("/register", methods=["GET"])
    async def register_page(request: Request):
        if getattr(request.state, "user_id", None):
            return RedirectResponse("/memories", status_code=302)
        return _render_with_csrf(request, "register.html")

    @mcp.custom_route("/register", methods=["POST"])
    async def register_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        confirm = form.get("password_confirm") or ""

        if not username or not password:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error="Username and password are required.",
            )
        if password != confirm:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error="Passwords do not match.",
            )

        try:
            await auth_db.create_user(username, password)
        except asyncpg.UniqueViolationError:
            return _render_with_csrf(
                request, "register.html",
                status_code=409,
                error="That username is taken.",
            )
        except ValueError as exc:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error=str(exc),
            )

        # Redirect to login rather than auto-login: forces the user to
        # confirm they remember the password they just typed.
        return RedirectResponse("/login", status_code=303)

    # -- Logout -----------------------------------------------------------

    @mcp.custom_route("/logout", methods=["POST"])
    async def logout_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        # Best-effort token revocation: find the cookie's token, resolve
        # it to a token row, mark revoked. If any step fails, we still
        # clear the cookie so the user is functionally logged out.
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie:
            resolved = await auth_db.resolve_token(cookie)
            if resolved:
                try:
                    await auth_db.revoke_token(
                        UUID(resolved["user_id"])
                        if isinstance(resolved["user_id"], str)
                        else resolved["user_id"],
                        UUID(resolved["token_id"]),
                    )
                except (ValueError, TypeError):
                    pass

        resp = RedirectResponse("/login", status_code=303)
        _clear_session_cookie(resp)
        return resp

    # -- Memories list ----------------------------------------------------

    @mcp.custom_route("/memories", methods=["GET"])
    async def memories_page(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        memories = await db.list_memories(owner_user_id=uid, limit=100)
        return _render_with_csrf(
            request, "memories.html",
            memories=memories, count=len(memories),
        )

    # -- Token management ------------------------------------------------

    @mcp.custom_route("/tokens", methods=["GET"])
    async def tokens_page(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        tokens_list = await auth_db.list_tokens(uid)
        return _render_with_csrf(
            request, "tokens.html",
            tokens=tokens_list, count=len(tokens_list),
        )

    @mcp.custom_route("/tokens", methods=["POST"])
    async def tokens_create(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        form = await request.form()
        label = (form.get("label") or "").strip() or None

        minted = await auth_db.create_token(uid, label=label)
        # Re-render the page with the raw value exposed as new_token.
        # The token is NOT stored server-side in this flow after this
        # render; if the user navigates away or reloads, the banner
        # disappears. Cookie-mint is idempotent on re-render.
        tokens_list = await auth_db.list_tokens(uid)
        return _render_with_csrf(
            request, "tokens.html",
            tokens=tokens_list, count=len(tokens_list),
            new_token=minted["token"],
            new_token_label=label,
        )

    @mcp.custom_route("/tokens/{token_id}", methods=["DELETE"])
    async def tokens_revoke(request: Request):
        redirect = _require_login(request)
        if redirect:
            return HTMLResponse("unauthorized", status_code=401)

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            token_uuid = UUID(request.path_params["token_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid token id", status_code=400)

        ok = await auth_db.revoke_token(uid, token_uuid)
        if not ok:
            return HTMLResponse("not found", status_code=404)

        # HTMX swap target is the row itself. Return empty body so the
        # row vanishes, matching how memory delete works. Alternative
        # would be returning an updated row showing the revoked state,
        # but that requires a partial-render handler; vanishing is
        # simpler and the full list is one click away.
        return HTMLResponse("", status_code=200)

    @mcp.custom_route("/memories/{memory_id}", methods=["DELETE"])
    async def memory_delete(request: Request):
        redirect = _require_login(request)
        if redirect:
            # HTMX DELETE to an unauthed endpoint: return 401 and let
            # the client decide. Redirects wouldn't be followed by HTMX.
            return HTMLResponse("unauthorized", status_code=401)

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            mid = UUID(request.path_params["memory_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid id", status_code=400)

        deleted = await db.forget_memory(mid, uid)
        if not deleted:
            return HTMLResponse("not found", status_code=404)

        # Empty body tells HTMX to remove the swap target (outerHTML
        # replaces with nothing). HTMX treats 200 + empty as the row
        # vanishing, which is exactly what we want.
        return HTMLResponse("", status_code=200)
