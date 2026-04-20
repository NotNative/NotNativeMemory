"""
Response-header hardening middleware.

Adds a baseline Content-Security-Policy and a short list of related
hardening headers to every HTTP response. CSP is the headline item:
defense-in-depth against XSS that complements Jinja2's autoescape.
If a future template bug ever lets user-controlled HTML escape the
escape layer, CSP prevents the injected script from running.

Policy shape:

    default-src 'self'
        Everything same-origin by default.

    script-src 'self' 'unsafe-inline' https://unpkg.com
        Same-origin scripts plus the htmx CDN. 'unsafe-inline' covers
        the theme pre-paint and toggle handler in base.html. Tighten
        by moving those out to static files + nonces later.

    style-src 'self' 'unsafe-inline'
        Inline <style> blocks in base.html and per-page templates.
        Same tightening path as script-src.

    img-src 'self' data:
        Same-origin images plus inline data URIs (harmless; defensive
        for any inline SVG / placeholder).

    object-src 'none'
        No Flash / Java / plugin embeds.

    base-uri 'self'
        Attackers cannot inject <base href=...> to retarget relative
        URLs.

    form-action 'self'
        Forms can only POST back to our origin (defense-in-depth for
        an injected <form action="evil.com">).

    frame-ancestors 'none'
        Clickjacking protection; replaces X-Frame-Options: DENY.

Companion headers:

    X-Content-Type-Options: nosniff
        Browsers must honor the Content-Type we declared; no MIME
        sniffing into script context.

    Referrer-Policy: same-origin
        Outbound links to other sites do not leak our paths.

    Permissions-Policy: camera=(), microphone=(), geolocation=()
        Explicitly disable sensor APIs we do not use.

Installation: outermost so headers land on every response including
the 413 synthesized by BodySizeLimitMiddleware and the 401 from
BearerAuthMiddleware.
"""

from __future__ import annotations

from typing import Iterable, Tuple


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_EXTRA_HEADERS: Tuple[Tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"content-security-policy", _CSP.encode("ascii")),
)


def _merge_headers(
    existing: Iterable[Tuple[bytes, bytes]],
) -> list[Tuple[bytes, bytes]]:
    """
    Keep whatever the downstream handler set; add our hardening
    headers only when absent. This lets a specific handler override
    (e.g. a future report-only CSP for an A/B) without the middleware
    fighting it.
    """
    have = {name.lower() for name, _ in existing}
    out = list(existing)
    for name, value in _EXTRA_HEADERS:
        if name not in have:
            out.append((name, value))
    return out


class SecurityHeadersMiddleware:
    """
    ASGI middleware that injects hardening headers on every HTTP
    response. Non-http scopes (websocket, lifespan) pass through.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        async def wrapped_send(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = _merge_headers(
                    message.get("headers", []),
                )
            await send(message)

        await self._app(scope, receive, wrapped_send)
