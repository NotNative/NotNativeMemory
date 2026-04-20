"""
Unit tests for lib/security_headers.py. No HTTP, no DB.

Exercises the ASGI middleware with a stub downstream app and verifies
the expected hardening headers appear on http.response.start messages.

Usage:
    python tests/test_security_headers.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import security_headers


async def _drive(scope):
    """Run the middleware against a trivial 200 app and return the
    response's headers as a lowercase-name dict."""
    captured = {"headers": []}

    async def app(scope, recv, snd):
        await snd({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html")],
        })
        await snd({"type": "http.response.body", "body": b"hi"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["headers"] = list(msg["headers"])

    mw = security_headers.SecurityHeadersMiddleware(app)
    await mw(scope, receive, send)
    return {name.decode("ascii").lower(): value.decode("ascii")
            for name, value in captured["headers"]}


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    # -- HTML response gets all hardening headers ---------------------------
    headers = asyncio.run(_drive({"type": "http", "headers": []}))

    check("CSP header present", "content-security-policy" in headers)
    check(
        "CSP includes default-src 'self'",
        "default-src 'self'" in headers.get("content-security-policy", ""),
    )
    check(
        "CSP allows htmx CDN script-src",
        "https://unpkg.com" in headers.get("content-security-policy", ""),
    )
    check(
        "CSP forbids frame-ancestors (clickjacking)",
        "frame-ancestors 'none'" in headers.get("content-security-policy", ""),
    )
    check(
        "CSP forbids object-src",
        "object-src 'none'" in headers.get("content-security-policy", ""),
    )

    check(
        "X-Content-Type-Options: nosniff set",
        headers.get("x-content-type-options") == "nosniff",
    )
    check(
        "Referrer-Policy set",
        headers.get("referrer-policy") == "same-origin",
    )
    check(
        "Permissions-Policy set",
        "camera=()" in headers.get("permissions-policy", ""),
    )
    check(
        "Content-Type preserved from downstream",
        headers.get("content-type") == "text/html",
    )

    # -- Non-http scope passes through (no headers touched) -----------------
    async def lifespan():
        ran = {"v": False}

        async def app(scope, recv, snd):
            ran["v"] = True

        await security_headers.SecurityHeadersMiddleware(app)(
            {"type": "lifespan"}, None, None,
        )
        return ran["v"]

    check("lifespan scope passes through", asyncio.run(lifespan()))

    # -- Existing header is preserved, not double-added ----------------------
    async def override_csp():
        captured = {"headers": []}

        async def app(scope, recv, snd):
            await snd({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-security-policy", b"default-src 'none'")],
            })
            await snd({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                captured["headers"] = list(msg["headers"])

        mw = security_headers.SecurityHeadersMiddleware(app)
        await mw({"type": "http", "headers": []}, receive, send)
        return captured["headers"]

    out = asyncio.run(override_csp())
    csp_count = sum(1 for n, _ in out if n.lower() == b"content-security-policy")
    csp_value = next((v for n, v in out if n.lower() == b"content-security-policy"), b"")
    check("downstream CSP override kept (single header)", csp_count == 1)
    check(
        "downstream CSP value preserved (not clobbered)",
        csp_value == b"default-src 'none'",
    )

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
