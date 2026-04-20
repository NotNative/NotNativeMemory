"""
Unit tests for lib/limits.py.

- Per-field helpers are tested directly.
- The ASGI middleware is tested by driving it through a stub receive/send
  pair that simulates content-length and streamed-body request patterns.

No DB, no network. Uses asyncio for the ASGI protocol dance.

Usage:
    python tests/test_limits.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import limits


async def _call_middleware(mw, scope, body_chunks):
    """
    Drive `mw(scope, receive, send)` to completion, feeding the given
    body chunks through receive. Returns (status_sent, body_sent, app_ran).
    """
    chunks = list(body_chunks)
    sent_status = {"code": None, "body": b""}
    app_ran = {"v": False}

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            more = bool(chunks)
            return {"type": "http.request", "body": chunk, "more_body": more}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent_status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            sent_status["body"] += msg.get("body", b"")

    async def app(scope, recv, snd):
        app_ran["v"] = True
        # Realistic downstream: reads the body then 200s.
        try:
            while True:
                m = await recv()
                if m["type"] == "http.disconnect":
                    raise RuntimeError("client disconnect")
                if not m.get("more_body"):
                    break
        except RuntimeError:
            # Middleware-triggered disconnect. Leave without sending.
            return
        await snd({"type": "http.response.start", "status": 200,
                   "headers": [(b"content-type", b"text/plain")]})
        await snd({"type": "http.response.body", "body": b"ok"})

    wrapped = mw(app)
    await wrapped(scope, receive, send)
    return sent_status["code"], sent_status["body"], app_ran["v"]


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    # -- enforce_field_len ---------------------------------------------------
    check("None passes through", limits.enforce_field_len(None, 100, "f") is None)
    check("short value passes", limits.enforce_field_len("abc", 100, "f") == "abc")

    ok = True
    try:
        limits.enforce_field_len("x" * 101, 100, "field")
        ok = False
    except limits.PayloadTooLarge:
        pass
    check("oversize raises PayloadTooLarge", ok)

    # UTF-8 byte-counted (not char-counted)
    mb = "€"  # 3 bytes in UTF-8
    ok = True
    try:
        limits.enforce_field_len(mb * 10, 20, "field")  # 30 bytes > 20
        ok = False
    except limits.PayloadTooLarge:
        pass
    check("multibyte chars counted by byte length", ok)

    # PayloadTooLarge is a ValueError so existing handlers catch it
    check(
        "PayloadTooLarge is a ValueError",
        issubclass(limits.PayloadTooLarge, ValueError),
    )

    # -- BodySizeLimitMiddleware: Content-Length reject path -----------------
    async def scenario_cl_reject():
        mw = lambda app: limits.BodySizeLimitMiddleware(app, max_bytes=100)
        scope = {
            "type": "http",
            "headers": [(b"content-length", b"500")],
        }
        return await _call_middleware(mw, scope, [b"x" * 50])

    status, body, app_ran = asyncio.run(scenario_cl_reject())
    check("Content-Length > cap rejects with 413", status == 413)
    check("CL reject: downstream app never runs", not app_ran)
    check("CL reject: body carries error JSON",
          b"request body too large" in body)

    # -- Content-Length under cap passes through -----------------------------
    async def scenario_cl_ok():
        mw = lambda app: limits.BodySizeLimitMiddleware(app, max_bytes=1000)
        scope = {
            "type": "http",
            "headers": [(b"content-length", b"50")],
        }
        return await _call_middleware(mw, scope, [b"x" * 50])

    status, _, app_ran = asyncio.run(scenario_cl_ok())
    check("Content-Length under cap: downstream runs", app_ran)
    check("Content-Length under cap: 200 returned", status == 200)

    # -- Streamed body (no CL) over cap: 413, app did not complete -----------
    async def scenario_stream_over():
        mw = lambda app: limits.BodySizeLimitMiddleware(app, max_bytes=10)
        scope = {"type": "http", "headers": []}
        # Three 5-byte chunks = 15 total, exceeds cap of 10
        return await _call_middleware(mw, scope, [b"aaaaa", b"bbbbb", b"ccccc"])

    status, body, _ = asyncio.run(scenario_stream_over())
    check("streamed body over cap rejects with 413", status == 413)
    check("streamed reject: error JSON present",
          b"request body too large" in body)

    # -- Streamed body under cap passes --------------------------------------
    async def scenario_stream_ok():
        mw = lambda app: limits.BodySizeLimitMiddleware(app, max_bytes=100)
        scope = {"type": "http", "headers": []}
        return await _call_middleware(mw, scope, [b"aaa", b"bbb"])

    status, _, app_ran = asyncio.run(scenario_stream_ok())
    check("streamed under cap: 200", status == 200)
    check("streamed under cap: app ran", app_ran)

    # -- Non-HTTP scope passes through (websocket, lifespan) -----------------
    async def scenario_lifespan():
        touched = {"v": False}

        async def app(scope, recv, snd):
            touched["v"] = True

        mw = limits.BodySizeLimitMiddleware(app, max_bytes=10)
        await mw({"type": "lifespan"}, None, None)
        return touched["v"]

    check("non-http scope bypasses cap", asyncio.run(scenario_lifespan()))

    # -- Malformed Content-Length does not crash -----------------------------
    async def scenario_malformed_cl():
        mw = lambda app: limits.BodySizeLimitMiddleware(app, max_bytes=100)
        scope = {
            "type": "http",
            "headers": [(b"content-length", b"not-a-number")],
        }
        return await _call_middleware(mw, scope, [b"small"])

    status, _, app_ran = asyncio.run(scenario_malformed_cl())
    check("malformed Content-Length: downstream still runs", app_ran)
    check("malformed Content-Length: 200", status == 200)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
