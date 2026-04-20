"""
Double-submit-cookie CSRF protection for the web GUI.

Applies to any state-changing HTTP method (POST, PUT, PATCH, DELETE)
that carries a session cookie but no `Authorization: Bearer` header.
API consumers with a Bearer token are exempt — they are not
browser-initiated and SameSite cookies don't apply.

Design:

    - A random 32-byte urlsafe value lives in the `nnm_csrf` cookie.
      The cookie is NOT HttpOnly because HTMX JavaScript reads it to
      send as a header on mutating calls.
    - The same value must appear in either the `_csrf` form field or
      the `X-CSRF-Token` header. If the two agree, the request is
      accepted. If they disagree, the request is rejected 403.
    - Missing cookie on a mutating call also rejects 403. The cookie
      is minted on any GET rendered through our template helpers, so
      any normal browsing flow has one.

    - SameSite=Lax on both the session cookie AND the CSRF cookie
      already mitigates most cross-site request forgery. Double-
      submit is defense-in-depth: even if a browser bug or a
      misconfiguration weakens SameSite, the attacker still needs
      to read our cookie (same-origin policy prevents it).

Usage:

    from lib.csrf import ensure_csrf, check_csrf, CSRF_COOKIE

    # On GET handlers that render a form:
    token = ensure_csrf(request, response)
    # Pass `token` into the template context as `csrf_token`.

    # On mutating handlers (called early, before parsing body):
    err = await check_csrf(request)
    if err is not None:
        return err  # 403 response
"""

from __future__ import annotations

import secrets
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


CSRF_COOKIE = "nnm_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "_csrf"
_TOKEN_BYTES = 32


def _gen_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def ensure_csrf(request: Request, response: Response) -> str:
    """
    Return the CSRF token for this request, minting one if the cookie
    is missing. Writes the cookie onto the response so the browser
    keeps it.

    Not HttpOnly because HTMX reads the value to send as a header.
    SameSite=Lax matches the session cookie.
    """
    existing = request.cookies.get(CSRF_COOKIE)
    if existing:
        return existing

    token = _gen_token()
    import os
    secure = os.environ.get("MEMORY_COOKIE_SECURE", "") in ("1", "true", "yes")
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return token


async def check_csrf(request: Request) -> Optional[Response]:
    """
    Validate the request's CSRF token. Returns None if the request
    passes; returns a 403 Response if it fails.

    Skip rules (request is NOT subject to CSRF):
      - Safe method (GET, HEAD, OPTIONS).
      - Bearer token in Authorization header (API consumer).

    Otherwise, the `nnm_csrf` cookie value must match the `_csrf`
    form field or the `X-CSRF-Token` header.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return None

    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not cookie_token:
        return JSONResponse({"error": "missing CSRF cookie"}, status_code=403)

    # Check header first — HTMX sends it there. For classic form
    # submissions we read the form body.
    header_token = request.headers.get(CSRF_HEADER, "")
    if header_token and secrets.compare_digest(header_token, cookie_token):
        return None

    # Form body check. We avoid consuming the body when the caller
    # already knows they're submitting a form; if the read races with
    # a downstream body read, Starlette caches the parsed form so
    # subsequent `await request.form()` calls get the same data.
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data"):
        form = await request.form()
        form_token = form.get(CSRF_FORM_FIELD, "")
        if form_token and secrets.compare_digest(form_token, cookie_token):
            return None

    return JSONResponse({"error": "CSRF check failed"}, status_code=403)
