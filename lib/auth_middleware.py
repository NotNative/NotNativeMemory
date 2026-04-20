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


_WHITELIST_PREFIXES = (
    "/auth/register",
    "/auth/login",
    "/health",
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


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
    return any(path.startswith(prefix) for prefix in _WHITELIST_PREFIXES)


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
            await self._try_attach_identity(request)
            return await call_next(request)

        # Solo-mode bypass: env says yes AND we have a named user to
        # act as AND the request is loopback. If any piece is missing,
        # fall through to token check.
        bypass_attached = await self._try_loopback_bypass(request)
        if bypass_attached:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing Authorization: Bearer <token>"},
                status_code=401,
            )

        token = header[len("bearer "):].strip()
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
        # Explicit token takes precedence over bypass.
        if request.headers.get("authorization", "").lower().startswith("bearer "):
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
        set_current_user_id(uid)
        return True

    async def _try_attach_identity(self, request: Request) -> None:
        """Best-effort token resolution for whitelisted paths."""
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return
        token = header[len("bearer "):].strip()
        from lib import auth_db
        resolved = await auth_db.resolve_token(token)
        if resolved is not None:
            request.state.user_id = resolved["user_id"]
            request.state.username = resolved["username"]
            set_current_user_id(resolved["user_id"])
