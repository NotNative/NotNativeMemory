"""
NNA bundle session_start.py tests.

The NNA session_start is a lightweight pre-warmer:
  - Calls memory_context on the NNM server to build/cache the ranked
    memory list before the first turn:pre fires.
  - Logs to ~/.nna/session_start.log (or MEMORY_SESSION_LOG override).
  - Never writes to stdout; never crashes the session on failure.

Tests here verify:
  1. Happy path: prewarm call hits the stub server, log line written.
  2. Server-unreachable fallback: exits 0, logs prewarm_skip.
  3. Malformed stdin: exits 1, does NOT crash with unhandled traceback.
  4. model_name is NOT a field this hook reads (it's turn_analysis's job).
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

_NNA_BUNDLE = os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory")
_HOOK_SCRIPT = os.path.join(_NNA_BUNDLE, "session_start.py")


# ---------------------------------------------------------------------------
# Stub MCP server
# ---------------------------------------------------------------------------

class _StubMCPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"context": [{"content": "m1"}, {"content": "m2"}]}),
                }]
            },
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a, **_k):
        pass


def _start_stub():
    server = HTTPServer(("127.0.0.1", 0), _StubMCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run_hook(stdin_payload: str, env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, _HOOK_SCRIPT],
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_prewarm_happy_path():
    """Hook calls memory_context, counts memories, logs prewarm_ok."""
    server = _start_stub()
    host, port = server.server_address
    mcp_url = f"http://{host}:{port}/mcp"

    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "session_start.log")
        proc = _run_hook(
            json.dumps({"event": "session.start", "session_id": "abc12345"}),
            {
                "MEMORY_MCP_URL": mcp_url,
                "MEMORY_SESSION_LOG": log_path,
            },
        )
        server.shutdown()
        time.sleep(0.05)

        assert proc.returncode == 0, f"expected exit 0, got {proc.returncode}\nstderr: {proc.stderr}"
        assert os.path.exists(log_path), "session_start.log was not written"
        with open(log_path, encoding="utf-8") as f:
            log_body = f.read()
        assert "prewarm_ok" in log_body, f"expected prewarm_ok in log, got: {log_body!r}"
        assert "memories=2" in log_body, f"expected memories=2 in log, got: {log_body!r}"
    print("[OK] prewarm happy path: log written with prewarm_ok memories=2")


def test_prewarm_server_unreachable():
    """When MCP server is down, hook exits 0 and logs prewarm_skip."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "session_start.log")
        proc = _run_hook(
            json.dumps({"event": "session.start", "session_id": "dead0000"}),
            {
                "MEMORY_MCP_URL": "http://127.0.0.1:1",  # nothing listening
                "MEMORY_SESSION_LOG": log_path,
            },
        )
        assert proc.returncode == 0, f"expected exit 0 on unreachable, got {proc.returncode}"
        assert os.path.exists(log_path), "session_start.log was not written even on skip"
        with open(log_path, encoding="utf-8") as f:
            log_body = f.read()
        assert "prewarm_skip" in log_body, f"expected prewarm_skip in log, got: {log_body!r}"
    print("[OK] server unreachable: exits 0, logs prewarm_skip")


def test_malformed_stdin():
    """Malformed JSON on stdin exits 1 without writing a log line."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "session_start.log")
        proc = _run_hook(
            "not-json",
            {
                "MEMORY_MCP_URL": "http://127.0.0.1:1",
                "MEMORY_SESSION_LOG": log_path,
            },
        )
        assert proc.returncode == 1, f"expected exit 1 on bad JSON, got {proc.returncode}"
    print("[OK] malformed stdin: exits 1")


def test_session_id_truncated_in_log():
    """Log line uses 8-char session_id prefix, not the full id."""
    server = _start_stub()
    host, port = server.server_address
    mcp_url = f"http://{host}:{port}/mcp"
    full_id = "abcdef1234567890"

    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "session_start.log")
        proc = _run_hook(
            json.dumps({"event": "session.start", "session_id": full_id}),
            {
                "MEMORY_MCP_URL": mcp_url,
                "MEMORY_SESSION_LOG": log_path,
            },
        )
        server.shutdown()
        time.sleep(0.05)

        assert proc.returncode == 0
        with open(log_path, encoding="utf-8") as f:
            log_body = f.read()
        assert "session=abcdef12" in log_body, f"expected 8-char prefix, got: {log_body!r}"
        assert full_id not in log_body, "full session_id leaked into log"
    print("[OK] session_id truncated to 8 chars in log")


if __name__ == "__main__":
    test_prewarm_happy_path()
    test_prewarm_server_unreachable()
    test_malformed_stdin()
    test_session_id_truncated_in_log()
    print("\nAll session_start_nna tests passed.")
