"""
Per-request user context for FastMCP tools.

FastMCP tools do not receive the HTTP Request object directly, so
the auth middleware stashes the authenticated user_id (if any) in a
module-level contextvars.ContextVar. Tools read it via
`current_user_id()` and use the value to tag writes and filter reads.

Design notes:

    - contextvars.ContextVar is asyncio-safe: every task gets its
      own copy, so two concurrent tool calls on behalf of different
      users do not cross-contaminate.

    - `None` is the legitimate "no identity" case (stdio mode, where
      the process is acting as the single-user owner of the host;
      or HTTP with localhost bypass and no token sent). Callers
      decide whether to treat None as "fall back to admin" or "skip
      write" based on context.

    - Only the user_id is stashed. Username and admin flag are kept
      on request.state because they are route-handler concerns; the
      tools only need identity for ownership tagging.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional
from uuid import UUID

_current_user_id: ContextVar[Optional[UUID]] = ContextVar(
    "nnm_current_user_id", default=None,
)


def set_current_user_id(user_id: Optional[UUID]) -> None:
    """Called by the auth middleware at the start of each request."""
    _current_user_id.set(user_id)


def current_user_id() -> Optional[UUID]:
    """Return the authenticated user's UUID, or None if unauthenticated."""
    return _current_user_id.get()
