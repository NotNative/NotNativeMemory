"""
Observability primitives for NotNativeMemory.

Two facilities:

1. Structured event log
   Every tool invocation emits one JSON line to the
   'notnative.events' logger. The line carries timestamp, user UUID
   (not username), tool name, outcome, latency, exception type on
   error, and a best-effort result size. Memory content, search
   queries, tags, and fact values never appear here.

2. In-memory Prometheus metrics
   Counters and histograms live in the default registry so they can
   be scraped via prometheus_client.generate_latest() from the
   /metrics route. Labels are intentionally low-cardinality (tool
   name, exception class, outcome, scope); no user IDs, no project
   IDs, no dynamic strings that could explode cardinality.

Overhead:
   Atomic counter increments and a single json.dumps per call.
   Measured as tens of microseconds on modern CPUs, negligible
   next to DB and embedding latency.
"""

import functools
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional
from uuid import UUID

from prometheus_client import Counter, Gauge, Histogram

_event_log = logging.getLogger("notnative.events")


# -- Recent-events ring buffer ----------------------------------------------
#
# Small in-memory deque that stores the last N tool-call events for the
# admin dashboard. Separate from the 'notnative.events' logger (which
# can be routed to stdout, a file, or syslog) so the dashboard never
# has to parse log output to show "what just happened".
#
# Capacity is small so the memory footprint is bounded. Operators who
# want more history configure the logger.
_RECENT_EVENTS_CAPACITY = 100
_recent_events: Deque[Dict[str, Any]] = deque(maxlen=_RECENT_EVENTS_CAPACITY)
_recent_events_lock = threading.Lock()


def recent_events(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return a snapshot of the most recent tool-call events, newest
    first. Safe to call from any thread; returns a copy so callers
    can't mutate the live buffer."""
    with _recent_events_lock:
        snapshot = list(_recent_events)
    snapshot.reverse()
    if limit is not None:
        snapshot = snapshot[:limit]
    return snapshot


# -- Metrics ----------------------------------------------------------------

tool_calls = Counter(
    "nnm_tool_calls_total",
    "Total MCP tool invocations.",
    ["tool", "outcome"],
)

tool_latency = Histogram(
    "nnm_tool_latency_seconds",
    "Latency of MCP tool invocations, in seconds.",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

tool_errors = Counter(
    "nnm_tool_errors_total",
    "MCP tool invocation errors by exception class.",
    ["tool", "exception_type"],
)

pool_connections_active = Gauge(
    "nnm_pool_connections_active",
    "Current count of checked-out asyncpg pool connections.",
)

pool_connections_idle = Gauge(
    "nnm_pool_connections_idle",
    "Current count of idle asyncpg pool connections.",
)


def _pool_active_value() -> float:
    """Scrape-time callback for the active-connections gauge. Lazy
    imports so this module can load before lib.db is ready."""
    try:
        from lib import db
        if db._pool is None:
            return 0
        return float(db._pool.get_size() - db._pool.get_idle_size())
    except Exception:
        return 0


def _pool_idle_value() -> float:
    try:
        from lib import db
        if db._pool is None:
            return 0
        return float(db._pool.get_idle_size())
    except Exception:
        return 0


pool_connections_active.set_function(_pool_active_value)
pool_connections_idle.set_function(_pool_idle_value)


# -- Snapshot for the admin dashboard ---------------------------------------

def metrics_snapshot() -> Dict[str, Any]:
    """
    Assemble a point-in-time view of our metrics, shaped for rendering
    in a Jinja template. Not Prometheus format; that is what /metrics
    already does. This is human-dashboard-shaped.

    Returns a dict with:
        tool_calls:   {tool_name: {"ok": int, "error": int, "total": int}}
        tool_latency: {tool_name: {"count": int, "sum_s": float, "avg_ms": float}}
        tool_errors:  {tool_name: {exception_type: int}}
        pool:         {"active": int, "idle": int}
    """
    from prometheus_client import REGISTRY

    snap: Dict[str, Any] = {
        "tool_calls": {},
        "tool_latency": {},
        "tool_errors": {},
        "pool": {"active": 0, "idle": 0},
    }

    for family in REGISTRY.collect():
        if not family.name.startswith("nnm_"):
            continue
        for sample in family.samples:
            labels = sample.labels or {}
            value = float(sample.value)

            if sample.name == "nnm_tool_calls_total":
                tool = labels.get("tool", "?")
                outcome = labels.get("outcome", "?")
                row = snap["tool_calls"].setdefault(
                    tool, {"ok": 0, "error": 0, "total": 0},
                )
                if outcome in row:
                    row[outcome] = int(value)
                row["total"] = row["ok"] + row["error"]

            elif sample.name == "nnm_tool_latency_seconds_count":
                tool = labels.get("tool", "?")
                row = snap["tool_latency"].setdefault(
                    tool, {"count": 0, "sum_s": 0.0, "avg_ms": 0.0},
                )
                row["count"] = int(value)

            elif sample.name == "nnm_tool_latency_seconds_sum":
                tool = labels.get("tool", "?")
                row = snap["tool_latency"].setdefault(
                    tool, {"count": 0, "sum_s": 0.0, "avg_ms": 0.0},
                )
                row["sum_s"] = value

            elif sample.name == "nnm_tool_errors_total":
                tool = labels.get("tool", "?")
                exc = labels.get("exception_type", "?")
                snap["tool_errors"].setdefault(tool, {})[exc] = int(value)

            elif sample.name == "nnm_pool_connections_active":
                snap["pool"]["active"] = int(value)

            elif sample.name == "nnm_pool_connections_idle":
                snap["pool"]["idle"] = int(value)

    for row in snap["tool_latency"].values():
        if row["count"] > 0:
            row["avg_ms"] = round((row["sum_s"] / row["count"]) * 1000, 2)

    return snap


# -- HTTP route registration ------------------------------------------------

def register_routes(mcp) -> None:
    """Register the /metrics scrape endpoint on the FastMCP app.

    Public by design: Prometheus scrapers typically do not carry
    per-user auth. The emitted payload is aggregate operational
    metadata (counts, histograms, gauges); no user IDs, project IDs,
    memory content, or search queries appear as label values. Match
    this posture with a firewall or reverse proxy if the server is
    reachable from untrusted networks.
    """
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(_request: Request):
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )


# -- Event-log helpers ------------------------------------------------------

def _log_event(**fields: Any) -> None:
    """Emit one structured JSON line to the events logger and append
    the same dict to the in-memory ring buffer.

    Defensive against unserializable types: any value that fails
    json.dumps falls back to its repr. We never want a telemetry
    failure to propagate and break a user-facing request.
    """
    try:
        line = json.dumps(fields, default=repr)
    except Exception:
        line = json.dumps({"ts": time.time(), "event_log_error": True})
    _event_log.info(line)

    try:
        with _recent_events_lock:
            _recent_events.append(dict(fields))
    except Exception:
        # Ring-buffer append must never break a tool call.
        pass


# -- Tool instrumentation decorator -----------------------------------------

def _result_size(result: Any) -> Optional[int]:
    """Best-effort size extraction for a tool's return dict, so the
    event log records how many items the call returned without
    logging the items themselves. Returns None if the tool's shape
    does not include a count.
    """
    if not isinstance(result, dict):
        return None
    for key in ("count",):
        if key in result and isinstance(result[key], int):
            return result[key]
    return None


def instrumented(tool_name: str) -> Callable:
    """
    Decorator that wraps an async MCP tool handler with timing,
    counters, and a structured log event.

    The wrapper inspects the handler's returned dict: if it contains
    an "error" key, the call is tallied as an error even though no
    exception left the function (tool handlers catch and return
    structured errors per Phase 3). Genuine uncaught exceptions still
    count as errors and are re-raised so the caller sees them.

    Applied below @mcp.tool() so FastMCP sees the wrapper's signature:

        @mcp.tool()
        @instrumented("memory_search")
        async def memory_search(...): ...

    functools.wraps preserves __name__, __doc__, and __wrapped__, so
    inspect.signature(...) keeps working and FastMCP builds the tool
    schema from the original signature.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve the caller lazily so we only pay the contextvar
            # lookup when we are about to log. Deferred import avoids
            # a circular reference at module load.
            from lib.auth_context import current_user_id

            start = time.monotonic()
            user_id = current_user_id()
            user_str = str(user_id) if isinstance(user_id, UUID) else None

            outcome = "ok"
            exception_type: Optional[str] = None
            result: Any = None
            raised = False

            try:
                result = await func(*args, **kwargs)
                if isinstance(result, dict) and "error" in result:
                    outcome = "error"
                    # Tools that went through server._tool_error stamp
                    # the returned dict with the concrete exception
                    # class so instrumentation can label tool_errors
                    # accurately even though the tool itself caught.
                    # Strip the private key before returning so MCP
                    # clients never see it.
                    if "_exception_type" in result:
                        exception_type = result.pop("_exception_type")
            except Exception as exc:
                raised = True
                outcome = "error"
                exception_type = type(exc).__name__
                raise
            finally:
                duration = time.monotonic() - start
                try:
                    tool_calls.labels(
                        tool=tool_name, outcome=outcome,
                    ).inc()
                    tool_latency.labels(tool=tool_name).observe(duration)
                    if exception_type:
                        tool_errors.labels(
                            tool=tool_name,
                            exception_type=exception_type,
                        ).inc()
                except Exception:
                    # Telemetry failures are non-fatal; prefer a
                    # working tool call over a clean metric.
                    pass

                _log_event(
                    ts=time.time(),
                    tool=tool_name,
                    user=user_str,
                    outcome=outcome,
                    latency_ms=round(duration * 1000, 2),
                    exception_type=exception_type,
                    result_size=(
                        _result_size(result) if not raised else None
                    ),
                )
            return result
        return wrapper
    return decorator


