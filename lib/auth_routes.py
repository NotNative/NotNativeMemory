"""
HTTP routes for the auth flow.

Call `register_routes(mcp)` once at server startup with the FastMCP
instance. The routes are registered via `mcp.custom_route`, which
exempts them from FastMCP's own auth layer so we can own the whole
perimeter via BearerAuthMiddleware.

Endpoints:

    POST /auth/register     create a user. First registrant becomes
                            admin and adopts pre-auth rows. After
                            the first user exists, this endpoint is
                            admin-only.
    POST /auth/login        exchange (username, password) for a new
                            Bearer token. The raw token is returned
                            exactly once.
    GET  /auth/tokens       list the authenticated caller's tokens
                            (metadata only, never the raw value or
                            the hash).
    POST /auth/tokens       mint a new token for the caller.
    DELETE /auth/tokens/{id}
                            revoke one of the caller's tokens.
    GET  /auth/me           echo the authenticated identity.

All handlers expect / produce JSON.
"""

from __future__ import annotations

import json
from uuid import UUID

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse

from lib import auth_db


async def _parse_json(request: Request) -> dict | None:
    try:
        body = await request.body()
        if not body:
            return {}
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _current_user_id(request: Request) -> UUID | None:
    """
    Read the user_id attached by BearerAuthMiddleware. Returns None
    when the request came through on the localhost bypass without a
    real token, which means no per-user operation is possible even if
    the bypass granted admin-equivalent permissions.
    """
    uid = getattr(request.state, "user_id", None)
    if uid is None:
        return None
    if isinstance(uid, UUID):
        return uid
    try:
        return UUID(str(uid))
    except (ValueError, TypeError):
        return None


def register_routes(mcp) -> None:
    """Attach all /auth/* routes to the FastMCP instance."""

    @mcp.custom_route("/auth/register", methods=["POST"])
    async def register(request: Request):
        payload = await _parse_json(request)
        if payload is None:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)

        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        if not username or not password:
            return JSONResponse(
                {"error": "username and password are required"},
                status_code=400,
            )

        existing = await auth_db.count_users()

        # First registrant on a fresh MCP becomes admin and adopts
        # every unowned row. Subsequent registrations require admin
        # auth — we read request.state.is_admin which was populated
        # by the middleware (either via Bearer or localhost bypass).
        if existing == 0:
            try:
                user = await auth_db.create_user(
                    username, password, is_admin=True,
                )
            except asyncpg.UniqueViolationError:
                return JSONResponse(
                    {"error": "username taken"}, status_code=409,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

            adoption = await auth_db.adopt_unowned_rows(UUID(user["id"]))
            return JSONResponse({
                "user": user,
                "adopted": adoption,
                "bootstrap_admin": True,
            }, status_code=201)

        # Non-bootstrap path: require an admin caller.
        if not getattr(request.state, "is_admin", False):
            return JSONResponse(
                {"error": "admin credentials required to register new users"},
                status_code=403,
            )

        try:
            user = await auth_db.create_user(
                username, password, is_admin=False,
            )
        except asyncpg.UniqueViolationError:
            return JSONResponse({"error": "username taken"}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return JSONResponse({"user": user}, status_code=201)

    @mcp.custom_route("/auth/login", methods=["POST"])
    async def login(request: Request):
        payload = await _parse_json(request)
        if payload is None:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)

        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        label = payload.get("label") or None

        record = await auth_db.get_user_by_username(username)
        # Auth-generic error so attackers cannot probe for valid usernames.
        from lib import auth
        if record is None:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
        if not auth.verify_secret(password, record["password_hash"]):
            return JSONResponse({"error": "invalid credentials"}, status_code=401)

        token = await auth_db.create_token(record["id"], label=label)
        return JSONResponse({
            "user": {
                "id": str(record["id"]),
                "username": record["username"],
                "is_admin": record["is_admin"],
            },
            "token": token,  # token["token"] is the raw value, shown once.
        }, status_code=200)

    @mcp.custom_route("/auth/tokens", methods=["GET", "POST"])
    async def tokens_collection(request: Request):
        uid = _current_user_id(request)
        if uid is None:
            return JSONResponse(
                {"error": "authenticated user required"}, status_code=401,
            )

        if request.method == "GET":
            items = await auth_db.list_tokens(uid)
            return JSONResponse({"tokens": items, "count": len(items)})

        # POST: mint a new token for the caller.
        payload = await _parse_json(request) or {}
        label = payload.get("label") or None
        token = await auth_db.create_token(uid, label=label)
        return JSONResponse({"token": token}, status_code=201)

    @mcp.custom_route("/auth/tokens/{token_id}", methods=["DELETE"])
    async def tokens_revoke(request: Request):
        uid = _current_user_id(request)
        if uid is None:
            return JSONResponse(
                {"error": "authenticated user required"}, status_code=401,
            )
        try:
            token_id = UUID(request.path_params["token_id"])
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid token id"}, status_code=400)

        ok = await auth_db.revoke_token(uid, token_id)
        if not ok:
            return JSONResponse(
                {"error": "token not found or already revoked"},
                status_code=404,
            )
        return JSONResponse({"revoked": True})

    @mcp.custom_route("/auth/me", methods=["GET"])
    async def me(request: Request):
        uid = _current_user_id(request)
        bypass = getattr(request.state, "auth_bypass", False)
        return JSONResponse({
            "user_id": str(uid) if uid else None,
            "username": getattr(request.state, "username", None),
            "is_admin": getattr(request.state, "is_admin", False),
            "localhost_bypass": bypass,
        })

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request):
        return JSONResponse({"status": "ok"})
