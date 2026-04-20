"""
Request body size enforcement for the HTTP surface.

Raw-ASGI middleware rejects any request whose body exceeds MAX_BODY_BYTES
with a 413 response. Two paths are covered:

    1. Content-Length header exceeds the cap: rejected before the
       downstream handler runs at all.
    2. No Content-Length (chunked / streaming): bytes are counted as
       they arrive. The first chunk that pushes total over the cap
       causes the wrapped receive() to return an http.disconnect, so
       the downstream handler sees an early termination and raises.
       The middleware then synthesizes a 413 response if the handler
       had not already started sending one.

This module also exposes per-field length constants and helpers that
the auth and memory paths use before trusting caller input. Per-field
limits catch a narrower attack (a valid-shape request with one field
sized to exhaust memory), which the global body cap alone cannot stop
once the body has been parsed.
"""

from __future__ import annotations

from typing import Callable


# ---- Global body cap ----

# 10 MiB covers legitimate requests with huge headroom (a typical
# memory payload is well under 1 KiB). Small enough that one malicious
# request cannot wedge the event loop on parse.
MAX_BODY_BYTES = 10 * 1024 * 1024

# ---- Per-field caps ----

# Memory content can be long (paragraphs / code snippets), so cap at
# 256 KiB. Past that a caller should be splitting into multiple
# memories anyway.
MAX_MEMORY_CONTENT_BYTES = 256 * 1024

# Tag and scope-like atoms are short identifiers.
MAX_TAG_BYTES = 128

# Fact triples are short phrases.
MAX_FACT_FIELD_BYTES = 512

# Usernames and passwords: generous enough for passphrases, bounded
# enough that hostile inputs cannot balloon the rate-limit store or
# force scrypt to run on outrageously long inputs.
MAX_USERNAME_BYTES = 64
MAX_PASSWORD_BYTES = 1024


class PayloadTooLarge(ValueError):
    """Raised when a user-provided field exceeds its per-field cap."""


def enforce_field_len(
    value: str | None, max_bytes: int, field_name: str,
) -> str | None:
    """
    Enforce a per-field byte length on caller-supplied text. None is
    passed through unchanged (absence is the caller's concern, not
    ours). UTF-8 encoded length is what we measure — a multi-byte
    character counts for what it is on the wire.
    """
    if value is None:
        return None
    if len(value.encode("utf-8")) > max_bytes:
        raise PayloadTooLarge(
            f"{field_name} exceeds {max_bytes}-byte cap",
        )
    return value


class BodySizeLimitMiddleware:
    """
    ASGI middleware that caps request body size. Install with:

        app.add_middleware(BodySizeLimitMiddleware)

    Accepts an optional `max_bytes` override in kwargs (tests use it).
    Only HTTP scope is filtered; WebSocket / lifespan pass through.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # Fast path: reject up-front if Content-Length exceeds the cap.
        # A malformed Content-Length is ignored here; downstream will
        # either parse successfully or fail with its own error.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    if int(value.decode("ascii")) > self._max:
                        await self._reject(send)
                        return
                except (ValueError, UnicodeDecodeError):
                    pass
                break

        # Slow path: wrap receive so streamed (chunked) bodies also
        # get capped. We count bytes per chunk and short-circuit via
        # http.disconnect when the cap is exceeded.
        consumed = 0
        exceeded = False

        async def capped_receive():
            nonlocal consumed, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                consumed += len(body)
                if consumed > self._max:
                    exceeded = True
                    # Signal end-of-stream so downstream handler stops
                    # consuming. Handler will typically treat this as
                    # a client disconnect and unwind.
                    return {"type": "http.disconnect"}
            return message

        started = {"flag": False}

        async def wrapped_send(message):
            if message.get("type") == "http.response.start":
                started["flag"] = True
            await send(message)

        try:
            await self._app(scope, capped_receive, wrapped_send)
        except Exception:
            # If the downstream raised because we cut its body short,
            # synthesize the 413 only if nothing has been sent yet.
            if exceeded and not started["flag"]:
                await self._reject(send)
                return
            raise

        # Downstream returned normally without ever sending a response
        # (edge case: early-return before reading body). Synthesize 413
        # only if we actually exceeded; otherwise the ASGI contract
        # requires the server to have sent something, which is not our
        # problem to fix.
        if exceeded and not started["flag"]:
            await self._reject(send)

    async def _reject(self, send) -> None:
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"error":"request body too large"}',
        })
