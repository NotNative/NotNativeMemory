"""
Starlette middleware that gates requests on a Bearer token.

Integration:
    The server wraps FastMCP's `streamable_http_app()` in a thin
    Starlette layer that adds this middleware. Every /mcp/* and
    /auth/tokens* request is checked; /auth/register and /auth/login
    (and /health) are whitelisted because they either bootstrap the
    auth flow or are public by design.

Localhost bypass:
    When the process reads `MEMORY_AUTH_LOCALHOST_BYPASS=1` and the
    request comes from a loopback address, auth is skipped and the
    request is treated as an anonymous admin-equivalent. This
    preserves the single-user zero-friction case. When the server
    binds to a non-loopback interface, flip the env var off (or leave
    it unset) so every call must present a token.

On success:
    request.state.user_id       UUID of the authenticated user
    request.state.username      login name
    request.state.is_admin      bool
    request.state.auth_bypass   True if localhost bypass fired
"""

from __future__ import annotations

import os
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# Paths that never require a token. Extend as needed; keep the list
# short — every entry is a hole in the auth perimeter.
_WHITELIST_PREFIXES = (
    "/auth/register",
    "/auth/login",
    "/health",
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _localhost_bypass_enabled() -> bool:
    """Read the env at call time so tests can toggle it without reload."""
    return os.environ.get("MEMORY_AUTH_LOCALHOST_BYPASS", "") in ("1", "true", "yes")


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    return request.client.host in _LOOPBACK_HOSTS


def _is_whitelisted(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _WHITELIST_PREFIXES)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Resolves an incoming Bearer token to a user and attaches identity
    to `request.state`. Rejects unauthenticated requests unless the
    route is whitelisted or localhost bypass is in effect.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if _is_whitelisted(path):
            # Whitelisted paths never REQUIRE a token, but if the
            # caller sends one we still resolve it and populate
            # request.state. That lets /auth/register differentiate
            # the "bootstrap first user" case (no token, no users)
            # from the "admin adds another user" case (admin token
            # present).
            request.state.auth_bypass = False
            request.state.user_id = None
            request.state.username = None
            request.state.is_admin = False
            await self._try_attach_identity(request)
            return await call_next(request)

        if _localhost_bypass_enabled() and _is_loopback(request):
            # Attach a best-effort identity: if the operator has
            # registered a user AND sent a token we still use it, but
            # anonymous loopback also works.
            request.state.auth_bypass = True
            request.state.user_id = None
            request.state.username = None
            request.state.is_admin = True
            await self._try_attach_identity(request)
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing Authorization: Bearer <token>"},
                status_code=401,
            )

        token = header[len("bearer "):].strip()
        # Local import: avoids pulling asyncpg / lib.db into tests that
        # only exercise middleware routing.
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
        request.state.is_admin = resolved["is_admin"]

        return await call_next(request)

    async def _try_attach_identity(self, request: Request) -> None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return
        token = header[len("bearer "):].strip()
        from lib import auth_db
        resolved = await auth_db.resolve_token(token)
        if resolved is not None:
            request.state.user_id = resolved["user_id"]
            request.state.username = resolved["username"]
            request.state.is_admin = resolved["is_admin"]
