"""
Regression: session_start.py must emit non-ASCII memory content on
Windows without crashing under the default cp1252 stdout encoding.

Reproduces the UnicodeEncodeError observed when a memory containing
'→' (right arrow) reaches sys.stdout.write on Windows. The fix
forces UTF-8 on sys.stdout/stderr at module import.

The test stubs the MCP server with a local HTTP server returning a
memory whose content contains an arrow, then runs the hook in a
subprocess with PYTHONIOENCODING unset and stdout pinned to cp1252
via PYTHONLEGACYWINDOWSSTDIO/explicit env. Asserts:
    1. Hook exits 0.
    2. stdout contains the arrow (or its replacement).
    3. last_error.log was not written for this run.
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

BUNDLES = [
    ("claude", os.path.join(ROOT, "hook_bundles", "claude", "notnative-memory")),
    ("nna", os.path.join(ROOT, "hook_bundles", "nna", "notnative-memory")),
]

ARROW = "→"
MEMORY_TEXT = f"LM Studio {ARROW} llama.cpp migration"


class _StubMCPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "context": [{"content": MEMORY_TEXT}],
                            "count": 1,
                        }),
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


def _start_stub():
    server = HTTPServer(("127.0.0.1", 0), _StubMCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run_hook(bundle_dir: str, mcp_url: str) -> subprocess.CompletedProcess:
    script = os.path.join(bundle_dir, "session_start.py")
    env = os.environ.copy()
    env["MEMORY_MCP_URL"] = mcp_url
    env["MEMORY_MCP_TOKEN"] = ""
    # Pin stdout to cp1252 so we reproduce the original crash if the
    # hook's own UTF-8 reconfigure is missing.
    env["PYTHONIOENCODING"] = "cp1252:strict"
    # capture_output uses binary pipes; decode ourselves to keep the
    # subprocess's encoding choice intact.
    return subprocess.run(
        [sys.executable, script],
        input=b'{"source":"startup","cwd":"."}',
        capture_output=True,
        timeout=10,
        env=env,
    )


def run() -> int:
    server = _start_stub()
    host, port = server.server_address
    mcp_url = f"http://{host}:{port}/mcp"

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    try:
        for label, src in BUNDLES:
            # Run against the bundle in place; the hook needs its
            # _internal/ siblings and hooks.env. Clear any stale error
            # log from prior runs of THIS test only.
            log_path = os.path.join(src, "last_error.log")
            had_log_before = os.path.exists(log_path)
            mtime_before = os.path.getmtime(log_path) if had_log_before else 0

            proc = _run_hook(src, mcp_url)

            check(f"{label}: hook exits 0", proc.returncode == 0)

            stdout_text = proc.stdout.decode("utf-8", errors="replace")
            check(
                f"{label}: stdout carries the arrow or its replacement",
                ARROW in stdout_text or "?" in stdout_text,
            )

            if proc.returncode != 0:
                print(f"  --- stderr ({label}) ---")
                print(proc.stderr.decode("utf-8", errors="replace"))

            mtime_after = os.path.getmtime(log_path) if os.path.exists(log_path) else 0
            check(
                f"{label}: no new traceback written to last_error.log",
                mtime_after == mtime_before,
            )
    finally:
        server.shutdown()
        # Give the daemon thread a beat to release the port.
        time.sleep(0.05)

    if failed:
        print(f"\n{failed} check(s) FAILED")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
