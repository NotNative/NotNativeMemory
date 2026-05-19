"""
Tests for hook_bundles/nna/notnative-memory/compaction_post.py.

The hook replaces the nna bundle's session.start:post path (deleted
2026-05-19): the only useful job session_start.py was doing was
post-compact memory recovery, but it wrote plain stdout and NNA's
shellHook only folds JSON-enveloped hookSpecificOutput.additionalContext
into payload.injected_context. This new hook fires on compaction:post
and emits the JSON envelope, so the recovery context actually lands.

Covers:
    1. Successful path: MCP returns context → hook emits envelope.
    2. Empty path: MCP returns no context → hook exits 0 with no stdout.
    3. Unicode safety: non-ASCII memories survive cp1252 stdout pinning.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
BUNDLE = os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory")
SCRIPT = os.path.join(BUNDLE, "compaction_post.py")

ARROW = "→"


class _StubMCP(BaseHTTPRequestHandler):
    """Returns whatever context list the test set on the server."""
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        context = getattr(self.server, "context", [])
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"context": context, "count": len(context)}),
                    }
                ]
            },
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a, **_k):
        pass


def _start_stub(context: list) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _StubMCP)
    server.context = context  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_hook(mcp_url: str, force_cp1252: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MEMORY_MCP_URL"] = mcp_url
    env["MEMORY_MCP_TOKEN"] = ""
    if force_cp1252:
        env["PYTHONIOENCODING"] = "cp1252:strict"
    return subprocess.run(
        [sys.executable, SCRIPT],
        input=b'{"event":"compaction","messages_removed":5,"cwd":"."}',
        capture_output=True,
        timeout=10,
        env=env,
    )


def _check(label: str, cond: bool, failed_counter: list) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        failed_counter[0] += 1


def run() -> int:
    failed = [0]

    # -- 1. Successful path emits the JSON envelope ------------------------
    server = _start_stub([
        {"content": "memory A"},
        {"content": "memory B"},
    ])
    host, port = server.server_address
    try:
        proc = _run_hook(f"http://{host}:{port}/mcp")
        _check("envelope: hook exits 0", proc.returncode == 0, failed)
        try:
            envelope = json.loads(proc.stdout.decode("utf-8"))
        except json.JSONDecodeError:
            envelope = None
        _check("envelope: stdout is valid JSON", envelope is not None, failed)
        if envelope:
            spec = envelope.get("hookSpecificOutput", {})
            _check(
                "envelope: hookEventName=PostCompact",
                spec.get("hookEventName") == "PostCompact",
                failed,
            )
            ctx = spec.get("additionalContext", "")
            _check(
                "envelope: additionalContext mentions both memories",
                "memory A" in ctx and "memory B" in ctx,
                failed,
            )
            _check(
                "envelope: header signals post-compact recovery",
                "Post-Compact Recovery" in ctx,
                failed,
            )
    finally:
        server.shutdown()
        time.sleep(0.05)

    # -- 2. Empty context → exit 0, no stdout ------------------------------
    server = _start_stub([])
    host, port = server.server_address
    try:
        proc = _run_hook(f"http://{host}:{port}/mcp")
        _check("empty: hook exits 0", proc.returncode == 0, failed)
        _check(
            "empty: no stdout when there is nothing to inject",
            proc.stdout.strip() == b"",
            failed,
        )
    finally:
        server.shutdown()
        time.sleep(0.05)

    # -- 3. Unicode safety: cp1252 stdout must not crash on arrows ---------
    server = _start_stub([
        {"content": f"LM Studio {ARROW} llama.cpp migration"},
    ])
    host, port = server.server_address
    try:
        proc = _run_hook(f"http://{host}:{port}/mcp", force_cp1252=True)
        _check("unicode: hook exits 0 under cp1252", proc.returncode == 0, failed)
        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        # json.dumps escapes non-ASCII to → by default; the literal arrow
        # only appears if the script switches to ensure_ascii=False. Either
        # form is acceptable here — the load-bearing check is that the
        # subprocess didn't crash on stdout encoding (cp1252 strict would
        # have raised UnicodeEncodeError before producing any output).
        _check(
            "unicode: stdout carries the arrow (literal or \\u2192-escaped)",
            ARROW in stdout_text or "\\u2192" in stdout_text,
            failed,
        )
    finally:
        server.shutdown()
        time.sleep(0.05)

    if failed[0]:
        print(f"\n{failed[0]} check(s) FAILED")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
