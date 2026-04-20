"""
Starlette middleware that gates requests on a Bearer token.

Integration:
    The server wraps FastMCP's `streamable_http_app()` in a thin
    Starlette layer that adds this middleware. Every /mcp/* and
    /auth/tokens* request is checked; /auth/register and /auth/login
    (and /health) are whitelisted because they either bootstrap the
    auth flow or are public by design.

Localhost bypass (solo mode):
    When the process reads both `MEMORY_AUTH_LOCALHOST_BYPASS=1` AND
    `MEMORY_AUTH_LOCALHOST_USER=<username>`, loopback callers are
    implicitly authenticated as the named user. No token required.
    This is the single-user deploy case: the operator runs the server
    bound to 127.0.0.1, and their hooks / local agents reach it from
    the same host. Writes land with that user's owner_user_id and
    reads filter the same way.

    When the env var is missing or the user does not exist, bypass is
    off and every non-whitelisted request needs a valid token.

    If the server binds to a non-loopback interface, flip
    MEMORY_AUTH_LOCALHOST_BYPASS off so every caller must present a
    token, regardless of who they claim to be.

On success:
    request.state.user_id     UUID of the authenticated user
    request.state.username    login name
    request.state.is_admin    True if the authenticated user has the
                              admin role (set via claim-admin or the
                              reset-admin CLI; no API surface toggles
                              it). Defaults False for token-bypass
                              paths that can't resolve a DB row.
    request.state.auth_bypass True if solo-mode bypass fired
"""

from __future__ import annotations

import os
from typing import Optional
from uuid import UUID

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from lib.auth_context import set_current_user_id


# Paths that never require authentication. Extend carefully; every
# entry is a hole in the auth perimeter. `/login` and `/register`
# cover both their GET (form page) and POST (submit) variants because
# the match is prefix-based. `/` is the front door that redirects
# logged-out callers to /login.
_WHITELIST_PREFIXES = (
    "/auth/register",
    "/auth/login",
    "/auth/claim-admin",
    "/health",
    "/login",
    "/register",
    "/claim-admin",
)

_WHITELIST_EXACT = ("/",)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Browser session cookie name. Must match the value used in
# lib/web_routes.py::SESSION_COOKIE_NAME.
_SESSION_COOKIE = "nnm_session"


def _localhost_bypass_enabled() -> bool:
    return os.environ.get("MEMORY_AUTH_LOCALHOST_BYPASS", "") in ("1", "true", "yes")


def _localhost_user() -> Optional[str]:
    """Username the bypass acts as, or None if unset."""
    val = os.environ.get("MEMORY_AUTH_LOCALHOST_USER", "").strip()
    return val or None


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    return request.client.host in _LOOPBACK_HOSTS


def _is_whitelisted(path: str) -> bool:
    if path in _WHITELIST_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _WHITELIST_PREFIXES)


def _read_bearer(request: Request) -> str:
    """
    Pull the Bearer token out of the request. Checks the
    `Authorization: Bearer ...` header first, then falls back to the
    `nnm_session` cookie set by the web login flow. Returns "" when
    nothing is present.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[len("bearer "):].strip()
    return request.cookies.get(_SESSION_COOKIE, "") or ""


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Resolves an incoming Bearer token or solo-mode bypass into a user
    and attaches identity to `request.state`. Rejects unauthenticated
    requests unless the route is whitelisted.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if _is_whitelisted(path):
            # Whitelisted paths never REQUIRE a token, but resolve
            # one if sent so handlers can see who's calling.
            request.state.auth_bypass = False
            request.state.user_id = None
            request.state.username = None
            request.state.is_admin = False
            await self._try_attach_identity(request)
            return await call_next(request)

        # Solo-mode bypass: env says yes AND we have a named user to
        # act as AND the request is loopback. If any piece is missing,
        # fall through to token check.
        bypass_attached = await self._try_loopback_bypass(request)
        if bypass_attached:
            return await call_next(request)

        token = _read_bearer(request)
        if not token:
            return JSONResponse(
                {"error": "missing Authorization: Bearer <token>"},
                status_code=401,
            )

        from lib import auth_db

        resolved = await auth_db.resolve_token(token)
        if resolved is None:
            return JSONResponse(
                {"error": "invalid or revoked token"},
                status_code=401,
            )

        request.state.auth_bypass = False
        request.state.user_id = resolved["user_id"]
        request.state.username = resolved["username"]
        request.state.is_admin = bool(resolved.get("is_admin", False))
        set_current_user_id(resolved["user_id"])

        return await call_next(request)

    async def _try_loopback_bypass(self, request: Request) -> bool:
        """
        Check whether the solo-mode bypass applies. Returns True if
        bypass fired and request.state is populated; False if the
        caller should fall through to token-based auth.

        An explicit Authorization header ALWAYS wins. A user who has
        a token for a different account can still use it from loopback
        without being silently overridden by the bypass identity.
        """
        if not _localhost_bypass_enabled():
            return False
        if not _is_loopback(request):
            return False
        # Explicit credentials take precedence over bypass. This covers
        # both an Authorization header (CLI / agents) and a session
        # cookie (browser users). A user holding a token for a
        # different account can still be recognized as that account.
        if _read_bearer(request):
            return False
        username = _localhost_user()
        if not username:
            return False

        from lib import auth_db
        user = await auth_db.get_user_by_username(username)
        if user is None:
            # Bypass is configured but pointing at a missing user. Fail
            # closed so the operator notices.
            return False

        uid: UUID = user["id"]
        request.state.auth_bypass = True
        request.state.user_id = uid
        request.state.username = user["username"]
        request.state.is_admin = bool(user.get("is_admin", False))
        set_current_user_id(uid)
        return True

    async def _try_attach_identity(self, request: Request) -> None:
        """Best-effort token resolution for whitelisted paths."""
        token = _read_bearer(request)
        if not token:
            return
        from lib import auth_db
        resolved = await auth_db.resolve_token(token)
        if resolved is not None:
            request.state.user_id = resolved["user_id"]
            request.state.username = resolved["username"]
            request.state.is_admin = bool(resolved.get("is_admin", False))
            set_current_user_id(resolved["user_id"])
