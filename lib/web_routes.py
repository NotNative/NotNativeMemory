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
    """Base template context. Adds the authenticated identity so nav renders."""
    ctx = {
        "request": request,
        "username": getattr(request.state, "username", None),
        "user_id": getattr(request.state, "user_id", None),
    }
    ctx.update(extra)
    return ctx


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
        return templates.TemplateResponse(
            "login.html", _context_for(request),
        )

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request):
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""

        record = await auth_db.get_user_by_username(username)
        if record is None or not auth.verify_secret(password, record["password_hash"]):
            return templates.TemplateResponse(
                "login.html",
                _context_for(request, error="Invalid username or password."),
                status_code=401,
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
        return templates.TemplateResponse(
            "register.html", _context_for(request),
        )

    @mcp.custom_route("/register", methods=["POST"])
    async def register_submit(request: Request):
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        confirm = form.get("password_confirm") or ""

        if not username or not password:
            return templates.TemplateResponse(
                "register.html",
                _context_for(request, error="Username and password are required."),
                status_code=400,
            )
        if password != confirm:
            return templates.TemplateResponse(
                "register.html",
                _context_for(request, error="Passwords do not match."),
                status_code=400,
            )

        try:
            await auth_db.create_user(username, password)
        except asyncpg.UniqueViolationError:
            return templates.TemplateResponse(
                "register.html",
                _context_for(request, error="That username is taken."),
                status_code=409,
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                "register.html",
                _context_for(request, error=str(exc)),
                status_code=400,
            )

        # Redirect to login rather than auto-login: forces the user to
        # confirm they remember the password they just typed.
        return RedirectResponse("/login", status_code=303)

    # -- Logout -----------------------------------------------------------

    @mcp.custom_route("/logout", methods=["POST"])
    async def logout_submit(request: Request):
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
        return templates.TemplateResponse(
            "memories.html",
            _context_for(request, memories=memories, count=len(memories)),
        )

    @mcp.custom_route("/memories/{memory_id}", methods=["DELETE"])
    async def memory_delete(request: Request):
        redirect = _require_login(request)
        if redirect:
            # HTMX DELETE to an unauthed endpoint: return 401 and let
            # the client decide. Redirects wouldn't be followed by HTMX.
            return HTMLResponse("unauthorized", status_code=401)

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
