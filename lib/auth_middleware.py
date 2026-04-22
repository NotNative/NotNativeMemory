"""
Starlette middleware that gates requests on a Bearer token.

Integration:
    The server wraps FastMCP's `streamable_http_app()` in a thin
    Starlette layer that adds this middleware. Every /mcp/* and
    /auth/tokens* request is checked; /auth/register and /auth/login
    (and /health) are whitelisted because they either bootstrap the
    auth flow or are public by design.

Single-user mode (default for fresh installs):
    Until the operator explicitly creates an admin, the server is in
    single-user mode. Every request authenticates as the `owner`
    sentinel user, regardless of source (loopback or LAN). No token
    required. Bind interface (loopback vs 0.0.0.0) does not matter.
    The operator transitions to multi-user by visiting the web GUI's
    "Enable Multi-User Mode" page, which writes a bootstrap token,
    accepts admin credentials + that token, and creates the first
    admin. From the next request onward, multi-user mode is active.

    Implicit signal: count_admins() == 0 means single-user; > 0 means
    multi-user. Cached for a short window to avoid a DB round trip on
    every request; invalidate_admin_cache() flips it the moment an
    admin is created.

Legacy localhost bypass:
    The older `MEMORY_AUTH_LOCALHOST_BYPASS=1` +
    `MEMORY_AUTH_LOCALHOST_USER=<username>` pair still works. It
    fires only after the single-user check, so a fresh install does
    not need it. It remains useful for installs that have already
    transitioned to multi-user but want loopback-on-server callers
    (hooks, server-side scripts) to act as a specific named user.

On success:
    request.state.user_id     UUID of the authenticated user
    request.state.username    login name
    request.state.is_admin    True if the authenticated user has the
                              admin role (set via claim-admin or the
                              reset-admin CLI; no API surface toggles
                              it). Defaults False for token-bypass
                              paths that can't resolve a DB row.
    request.state.auth_bypass True if single-user or localhost bypass fired
"""

from __future__ import annotations

import os
import time
from typing import Optional
from uuid import UUID

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from lib.auth_context import set_current_user_id


# Cache the "any admin exists?" flag for a few seconds so the auth
# middleware doesn't issue a SELECT on every request. The flag rarely
# changes (once per install, on the single-user -> multi-user flip),
# and invalidate_admin_cache() forces a re-check immediately when it
# does. TTL is conservative: even with no explicit invalidation, the
# operator sees the new behavior within this many seconds.
_ADMIN_CACHE_TTL_SECONDS = 5
_admin_cache: dict = {"any_admin": None, "expires_at": 0.0}


def invalidate_admin_cache() -> None:
    """Force the next request to re-check whether any admin exists.

    Called after an admin is created or deleted so the middleware
    transitions between single-user and multi-user mode within one
    request rather than waiting up to _ADMIN_CACHE_TTL_SECONDS for
    the cached value to expire.
    """
    _admin_cache["any_admin"] = None
    _admin_cache["expires_at"] = 0.0


async def _any_admin_exists() -> bool:
    """Cached single-user-vs-multi-user check."""
    now = time.monotonic()
    if (
        _admin_cache["any_admin"] is not None
        and _admin_cache["expires_at"] > now
    ):
        return _admin_cache["any_admin"]

    from lib import auth_db
    count = await auth_db.count_admins()
    _admin_cache["any_admin"] = count > 0
    _admin_cache["expires_at"] = now + _ADMIN_CACHE_TTL_SECONDS
    return _admin_cache["any_admin"]


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
    "/metrics",
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
            # one if sent so handlers can see who's calling. In
            # single-user mode they also pick up the owner identity
            # so handlers like the index page can render
            # "logged in as owner" affordances.
            request.state.auth_bypass = False
            request.state.user_id = None
            request.state.username = None
            request.state.is_admin = False
            if await self._try_single_user_bypass(request):
                return await call_next(request)
            await self._try_attach_identity(request)
            return await call_next(request)

        # Single-user mode: zero admins means every caller (including
        # LAN clients) authenticates as the owner sentinel. Default
        # for fresh installs; the operator opts into multi-user via
        # the web GUI by claiming the first admin.
        if await self._try_single_user_bypass(request):
            return await call_next(request)

        # Legacy localhost bypass: applies only after multi-user has
        # been turned on. A loopback caller with bypass env vars set
        # can still impersonate a named non-admin user without a token.
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

    async def _try_single_user_bypass(self, request: Request) -> bool:
        """
        Authenticate every caller as the owner sentinel when no admin
        exists. Returns True if bypass fired; False to fall through.

        An explicit Bearer token still wins: a holder of a token (which
        cannot exist until at least one admin has been claimed, but
        could survive a deliberate admin demotion) is recognized as
        themselves.

        Sets request.state.single_user_mode = True on the bypass path
        so templates can render the "Switch to Multi-User Mode"
        affordance unconditionally.
        """
        request.state.single_user_mode = False
        if await _any_admin_exists():
            return False
        if _read_bearer(request):
            # Non-empty Bearer in single-user mode is unusual, but a
            # tester could send one. Fall through so resolve_token can
            # accept or reject it on its merits.
            return False

        from lib import auth_db
        owner = await auth_db.ensure_owner_sentinel()
        owner_uid: UUID = owner["id"] if isinstance(owner["id"], UUID) else UUID(owner["id"])

        request.state.auth_bypass = True
        request.state.single_user_mode = True
        request.state.user_id = owner_uid
        request.state.username = owner["username"]
        # Sentinel is intentionally NOT admin. The whole point of
        # single-user mode is that no admin exists yet.
        request.state.is_admin = False
        set_current_user_id(owner_uid)
        return True

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
