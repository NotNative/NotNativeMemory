"""
HTML routes for the web admin / user GUI.

Mounted on the same Starlette app as the MCP and /auth/* routes via
`register_routes(mcp)`. Uses BearerAuthMiddleware for auth, with a
cookie fallback so browsers don't have to send the Authorization
header on every page load.

First slice: login / register / logout, memory list + delete, token
management. Edit, rescope, bulk ops, and filters come in later slices.
"""

from __future__ import annotations

import functools
import logging
import os
from uuid import UUID

import asyncpg
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from lib import admin_bootstrap, audit, auth, auth_db, password_policy, rate_limit
from lib.csrf import check_csrf, get_or_mint_csrf, set_csrf_cookie


_log = logging.getLogger("notnative.web")

# Cap on how many memories a single bulk-delete form submission can
# target. Stops a hostile or buggy client from shoveling the entire
# DB into one request and turning a 400ms DELETE into a 40s one.
_BULK_DELETE_LIMIT = 100


# -- Template loader --------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_TEMPLATE_DIR = os.path.join(_ROOT, "templates")

templates = Jinja2Templates(directory=_TEMPLATE_DIR)


# -- Session cookie ---------------------------------------------------------

SESSION_COOKIE_NAME = "nnm_session"


def _cookie_secure() -> bool:
    """
    Should session cookies carry the Secure attribute? On by default
    behind a real TLS-terminating proxy; off for local HTTP dev. The
    env var is read per-request so tests can flip it without a reload.
    """
    return os.environ.get("MEMORY_COOKIE_SECURE", "") in ("1", "true", "yes")


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# -- View helpers -----------------------------------------------------------


def _context_for(request: Request, csrf_token: str, **extra) -> dict:
    """Base template context. Adds the authenticated identity so nav renders."""
    ctx = {
        "username": getattr(request.state, "username", None),
        "user_id": getattr(request.state, "user_id", None),
        "is_admin": bool(getattr(request.state, "is_admin", False)),
        "single_user_mode": bool(getattr(request.state, "single_user_mode", False)),
        "csrf_token": csrf_token,
    }
    ctx.update(extra)
    return ctx


def _render_with_csrf(
    request: Request, template: str, status_code: int = 200, **extra,
):
    """
    Render a template with a guaranteed-present csrf_token in context.
    Mints the CSRF cookie once and renders once with the same value,
    so the token the form carries always matches the cookie the browser
    receives (even on a cold visit).
    """
    token, is_new = get_or_mint_csrf(request)
    ctx = _context_for(request, csrf_token=token, **extra)
    response = templates.TemplateResponse(
        request, template, context=ctx, status_code=status_code,
    )
    if is_new:
        set_csrf_cookie(response, token)
    return response


def _require_login(request: Request) -> RedirectResponse | None:
    """Return a redirect response if not logged in, else None."""
    if not getattr(request.state, "user_id", None):
        return RedirectResponse("/login", status_code=302)
    return None


def _require_admin(request: Request) -> HTMLResponse | RedirectResponse | None:
    """
    Guard for /admin/* pages. Returns:
      - RedirectResponse to /login when the caller isn't logged in.
      - HTMLResponse 403 when the caller is logged in but not admin.
      - None when the caller is an authenticated admin.

    Usage pattern in handlers:

        reject = _require_admin(request)
        if reject:
            return reject
    """
    redirect = _require_login(request)
    if redirect:
        return redirect
    if not getattr(request.state, "is_admin", False):
        return HTMLResponse(
            "Forbidden: admin role required.", status_code=403,
        )
    return None


def _qs_str(params, name: str) -> str | None:
    """Return trimmed query-param ``name``, or None if missing/blank."""
    value = params.get(name, "").strip()
    return value or None


def _qs_int(params, name: str, default: int) -> int:
    """Return query-param ``name`` coerced to int, else ``default``."""
    try:
        return int(params.get(name, ""))
    except (TypeError, ValueError):
        return default


def _pagination_ctx(
    *,
    offset: int,
    limit: int,
    total: int,
    filters: dict,
    filter_names,
    extra_pairs=(),
) -> dict:
    """
    Compute prev/next offsets and the querystring-minus-offset that
    every paginated admin/user page passes to its template.

    `filter_names` is the ordered iterable of filter keys appended when
    their value is truthy. `extra_pairs` is an iterable of (k, v) tuples
    appended unconditionally (used for boolean flags that render as k=1).
    ``limit`` is always appended last.
    """
    parts = []
    for name in filter_names:
        value = filters.get(name)
        if value:
            parts.append(f"{name}={value}")
    for k, v in extra_pairs:
        parts.append(f"{k}={v}")
    parts.append(f"limit={limit}")
    return {
        "prev_offset": max(0, offset - limit) if offset > 0 else None,
        "next_offset": offset + limit if offset + limit < total else None,
        "qs_without_offset": "&".join(parts),
    }


# -- Route guards -----------------------------------------------------------
#
# Each of these wraps the handler's preamble. The five variants exist
# because unauthed requests on GET/form-POST routes get a redirect (the
# user's browser will follow it), while XHR DELETEs get a 401 (HTMX
# won't follow redirects).


def require_login(handler):
    """GET page guard: redirect unauthenticated users to /login."""
    @functools.wraps(handler)
    async def wrapper(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect
        return await handler(request)
    return wrapper


def require_admin(handler):
    """GET page guard: 403 for non-admins, redirect for anonymous users."""
    @functools.wraps(handler)
    async def wrapper(request: Request):
        reject = _require_admin(request)
        if reject:
            return reject
        return await handler(request)
    return wrapper


def require_login_csrf(handler):
    """Form-POST guard: login + CSRF; redirect on unauth."""
    @functools.wraps(handler)
    async def wrapper(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err
        return await handler(request)
    return wrapper


def require_admin_csrf(handler):
    """Form-POST guard: admin + CSRF; 403/redirect on unauth."""
    @functools.wraps(handler)
    async def wrapper(request: Request):
        reject = _require_admin(request)
        if reject:
            return reject
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err
        return await handler(request)
    return wrapper


def require_login_csrf_xhr(handler):
    """XHR/DELETE guard: login + CSRF; 401 plain response on unauth.

    HTMX won't follow a 302 on a DELETE, so returning a redirect is
    useless. 401 lets the client decide whether to reload.
    """
    @functools.wraps(handler)
    async def wrapper(request: Request):
        redirect = _require_login(request)
        if redirect:
            return HTMLResponse("unauthorized", status_code=401)
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err
        return await handler(request)
    return wrapper


# -- Routes -----------------------------------------------------------------


def register_routes(mcp) -> None:
    """Attach all HTML / form routes to the FastMCP instance."""

    @mcp.custom_route("/", methods=["GET"])
    async def root(request: Request):
        # Single-user mode: middleware has already authenticated as the
        # owner sentinel, so go straight to the dashboard. The banner
        # in base.html offers the "Switch to Multi-User Mode" path.
        if getattr(request.state, "single_user_mode", False):
            return RedirectResponse("/memories", status_code=302)
        # Mid-transition: bootstrap file exists but no admin yet means
        # the operator clicked "enable multi-user" and is in the middle
        # of the claim form. Send them to the form.
        if admin_bootstrap.bootstrap_file_exists():
            return RedirectResponse("/enable-multiuser", status_code=302)
        if getattr(request.state, "user_id", None):
            return RedirectResponse("/memories", status_code=302)
        return RedirectResponse("/login", status_code=302)

    # -- Login ------------------------------------------------------------

    @mcp.custom_route("/login", methods=["GET"])
    async def login_page(request: Request):
        if getattr(request.state, "user_id", None):
            return RedirectResponse("/memories", status_code=302)
        return _render_with_csrf(request, "login.html")

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""

        ip = rate_limit.client_ip(request)
        allowed, retry = rate_limit.check_login(ip, username)
        if not allowed:
            await audit.log_event(
                "login.rate_limited",
                detail={**audit.request_detail(request), "username_tried": username[:64]},
            )
            wait = max(1, int(retry + 0.999))
            resp = _render_with_csrf(
                request, "login.html",
                status_code=429,
                error=f"Too many login attempts. Try again in {wait}s.",
            )
            resp.headers["Retry-After"] = str(wait)
            return resp

        record = await auth_db.get_user_by_username(username)
        # verify_or_dummy runs scrypt against a fixed hash when the user
        # does not exist, so response time does not distinguish "no
        # such user" from "bad password". Username enumeration via
        # timing is what this closes.
        stored = record["password_hash"] if record else None
        if not auth.verify_or_dummy(password, stored):
            rate_limit.record_login_failure(ip, username)
            await audit.log_event(
                "login.fail",
                actor_user_id=record["id"] if record else None,
                detail={**audit.request_detail(request), "username_tried": username[:64]},
            )
            return _render_with_csrf(
                request, "login.html",
                status_code=401,
                error="Invalid username or password.",
            )

        rate_limit.clear_login(ip, username)
        token = await auth_db.create_token(record["id"], label="web-session")
        await audit.log_event(
            "login.success",
            actor_user_id=record["id"],
            target_id=UUID(token["id"]),
            detail={**audit.request_detail(request), "label": "web-session"},
        )
        resp = RedirectResponse("/memories", status_code=303)
        _set_session_cookie(resp, token["token"])
        return resp

    # -- Claim admin ------------------------------------------------------

    @mcp.custom_route("/claim-admin", methods=["GET"])
    async def claim_admin_page(request: Request):
        # Only available while the bootstrap file exists. Once an admin
        # is claimed the file is deleted and this page 404s so casual
        # visitors don't get an enticing target.
        if not admin_bootstrap.bootstrap_file_exists():
            return RedirectResponse("/login", status_code=302)
        return _render_with_csrf(request, "claim_admin.html")

    @mcp.custom_route("/claim-admin", methods=["POST"])
    async def claim_admin_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        # Re-check file existence on the submit path too; a race where
        # another claim raced in and succeeded should not let a second
        # claim slip through.
        if not admin_bootstrap.bootstrap_file_exists():
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        bootstrap_token = (form.get("bootstrap_token") or "").strip()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        confirm = form.get("password_confirm") or ""

        if not bootstrap_token or not username or not password:
            return _render_with_csrf(
                request, "claim_admin.html",
                status_code=400,
                error="Bootstrap token, username, and password are required.",
            )
        if password != confirm:
            return _render_with_csrf(
                request, "claim_admin.html",
                status_code=400,
                error="Passwords do not match.",
            )

        if not admin_bootstrap.validate_bootstrap_token(bootstrap_token):
            await audit.log_event(
                "admin.claim_fail",
                detail=audit.request_detail(request),
            )
            return _render_with_csrf(
                request, "claim_admin.html",
                status_code=401,
                error="Invalid bootstrap token.",
            )

        policy_err = await password_policy.validate_new_password(password)
        if policy_err:
            return _render_with_csrf(
                request, "claim_admin.html",
                status_code=400,
                error=policy_err,
            )

        try:
            new_user = await auth_db.create_user(username, password)
        except asyncpg.UniqueViolationError:
            return _render_with_csrf(
                request, "claim_admin.html",
                status_code=409,
                error="That username is taken.",
            )
        except ValueError as exc:
            return _render_with_csrf(
                request, "claim_admin.html",
                status_code=400,
                error=str(exc),
            )

        # Promote and delete the file before minting the session token
        # so a mid-flow crash never leaves us with a non-admin account
        # and a still-valid bootstrap file.
        uid = UUID(new_user["id"])
        await auth_db.set_admin(uid, True)
        admin_bootstrap.delete_bootstrap_file()

        await audit.log_event(
            "admin.claimed",
            actor_user_id=uid,
            detail=audit.request_detail(request),
        )
        await audit.log_event(
            "user.register",
            actor_user_id=uid,
            detail={**audit.request_detail(request), "is_admin": True},
        )

        token = await auth_db.create_token(uid, label="web-session")
        await audit.log_event(
            "login.success",
            actor_user_id=uid,
            target_id=UUID(token["id"]),
            detail={**audit.request_detail(request), "label": "web-session"},
        )

        resp = RedirectResponse("/memories", status_code=303)
        _set_session_cookie(resp, token["token"])
        return resp

    # -- Enable multi-user mode (transition out of single-user) -----------

    @mcp.custom_route("/enable-multiuser", methods=["GET"])
    async def enable_multiuser_page(request: Request):
        # Multi-user is the absence of single-user. If an admin already
        # exists, this page has nothing to offer; bounce to login.
        if not getattr(request.state, "single_user_mode", False):
            return RedirectResponse("/login", status_code=302)
        # Lazy-write the bootstrap token. The eager path was removed when
        # single-user mode landed; this route is the only writer now.
        path = await admin_bootstrap.ensure_bootstrap_if_needed()
        if path:
            admin_bootstrap.log_bootstrap_banner(path)
        return _render_with_csrf(request, "enable_multiuser.html")

    @mcp.custom_route("/enable-multiuser", methods=["POST"])
    async def enable_multiuser_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        # Re-check on submit: a concurrent claim from another tab
        # might have already flipped to multi-user. Treat that as a
        # success path -> /login.
        if not getattr(request.state, "single_user_mode", False):
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        bootstrap_token = (form.get("bootstrap_token") or "").strip()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        confirm = form.get("password_confirm") or ""

        if not bootstrap_token or not username or not password:
            return _render_with_csrf(
                request, "enable_multiuser.html",
                status_code=400,
                error="Bootstrap token, username, and password are required.",
            )
        if password != confirm:
            return _render_with_csrf(
                request, "enable_multiuser.html",
                status_code=400,
                error="Passwords do not match.",
            )
        if username == auth_db.OWNER_SENTINEL_USERNAME:
            return _render_with_csrf(
                request, "enable_multiuser.html",
                status_code=400,
                error=(
                    f"'{auth_db.OWNER_SENTINEL_USERNAME}' is reserved for "
                    f"the single-user sentinel. Pick a different admin "
                    f"username."
                ),
            )

        policy_err = await password_policy.validate_new_password(password)
        if policy_err:
            return _render_with_csrf(
                request, "enable_multiuser.html",
                status_code=400,
                error=policy_err,
            )

        try:
            result = await auth_db.claim_admin_and_transfer_data(
                bootstrap_token, username, password,
            )
        except ValueError as exc:
            await audit.log_event(
                "admin.claim_fail",
                detail={**audit.request_detail(request), "reason": str(exc)},
            )
            return _render_with_csrf(
                request, "enable_multiuser.html",
                status_code=401,
                error=str(exc),
            )
        except asyncpg.UniqueViolationError:
            return _render_with_csrf(
                request, "enable_multiuser.html",
                status_code=409,
                error="That username is taken.",
            )

        # Cutover: the next request should see multi-user mode.
        from lib import auth_middleware
        auth_middleware.invalidate_admin_cache()

        admin = result["admin"]
        uid = UUID(admin["id"])

        await audit.log_event(
            "admin.claimed",
            actor_user_id=uid,
            detail={
                **audit.request_detail(request),
                "transferred": result.get("transferred", {}),
                "via": "enable-multiuser",
            },
        )
        await audit.log_event(
            "user.register",
            actor_user_id=uid,
            detail={**audit.request_detail(request), "is_admin": True},
        )

        token = await auth_db.create_token(uid, label="web-session")
        await audit.log_event(
            "login.success",
            actor_user_id=uid,
            target_id=UUID(token["id"]),
            detail={**audit.request_detail(request), "label": "web-session"},
        )

        resp = RedirectResponse("/memories", status_code=303)
        _set_session_cookie(resp, token["token"])
        return resp

    # -- Register ---------------------------------------------------------

    @mcp.custom_route("/register", methods=["GET"])
    async def register_page(request: Request):
        if getattr(request.state, "user_id", None):
            return RedirectResponse("/memories", status_code=302)
        return _render_with_csrf(request, "register.html")

    @mcp.custom_route("/register", methods=["POST"])
    async def register_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        ip = rate_limit.client_ip(request)
        allowed, retry = rate_limit.check_register(ip)
        if not allowed:
            wait = max(1, int(retry + 0.999))
            resp = _render_with_csrf(
                request, "register.html",
                status_code=429,
                error=f"Too many registration attempts. Try again in {wait}s.",
            )
            resp.headers["Retry-After"] = str(wait)
            return resp

        # Count the attempt regardless of outcome so a hostile script
        # cannot spray registrations at our rate limit for free by
        # hitting only failure paths.
        rate_limit.record_register_attempt(ip)

        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        confirm = form.get("password_confirm") or ""

        if not username or not password:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error="Username and password are required.",
            )
        if password != confirm:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error="Passwords do not match.",
            )

        policy_err = await password_policy.validate_new_password(password)
        if policy_err:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error=policy_err,
            )

        try:
            new_user = await auth_db.create_user(username, password)
        except asyncpg.UniqueViolationError:
            return _render_with_csrf(
                request, "register.html",
                status_code=409,
                error="That username is taken.",
            )
        except ValueError as exc:
            return _render_with_csrf(
                request, "register.html",
                status_code=400,
                error=str(exc),
            )

        await audit.log_event(
            "user.register",
            actor_user_id=UUID(new_user["id"]),
            detail=audit.request_detail(request),
        )

        # Redirect to login rather than auto-login: forces the user to
        # confirm they remember the password they just typed.
        return RedirectResponse("/login", status_code=303)

    # -- Admin: users ------------------------------------------------------

    @mcp.custom_route("/admin/users", methods=["GET"])
    @require_admin
    async def admin_users_page(request: Request):
        params = request.query_params
        search = (params.get("search") or "").strip() or None
        limit = max(1, min(_qs_int(params, "limit", 50), 200))
        offset = max(0, _qs_int(params, "offset", 0))

        users, total = await auth_db.list_users_overview(
            offset=offset, limit=limit, search=search,
        )

        page = _pagination_ctx(
            offset=offset, limit=limit, total=total,
            filters={"search": search}, filter_names=("search",),
        )

        return _render_with_csrf(
            request, "admin_users.html",
            users=users, count=len(users), total=total,
            offset=offset, limit=limit, search=search,
            **page,
        )

    @mcp.custom_route("/admin/users/{user_id}/force-logout", methods=["POST"])
    @require_admin_csrf
    async def admin_users_force_logout(request: Request):
        try:
            target = UUID(request.path_params["user_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid user id", status_code=400)

        try:
            new_gen = await auth_db.bump_token_generation(target)
        except ValueError:
            return HTMLResponse("user not found", status_code=404)

        acting = request.state.user_id
        if isinstance(acting, str):
            acting = UUID(acting)
        await audit.log_event(
            "admin.force_logout",
            actor_user_id=acting,
            target_id=target,
            detail={**audit.request_detail(request), "new_generation": new_gen},
        )
        return HTMLResponse("", status_code=200)

    @mcp.custom_route("/admin/users/{user_id}/password", methods=["GET"])
    @require_admin
    async def admin_users_password_page(request: Request):
        try:
            target = UUID(request.path_params["user_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid user id", status_code=400)
        user = await auth_db.get_user_by_id(target)
        if user is None:
            return HTMLResponse("user not found", status_code=404)
        return _render_with_csrf(
            request, "admin_user_password.html", target_user=user,
        )

    @mcp.custom_route("/admin/users/{user_id}/password", methods=["POST"])
    @require_admin_csrf
    async def admin_users_password_submit(request: Request):
        try:
            target = UUID(request.path_params["user_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid user id", status_code=400)

        user = await auth_db.get_user_by_id(target)
        if user is None:
            return HTMLResponse("user not found", status_code=404)

        form = await request.form()
        new_password = form.get("new_password") or ""
        confirm = form.get("password_confirm") or ""

        if not new_password:
            return _render_with_csrf(
                request, "admin_user_password.html",
                target_user=user, status_code=400,
                error="New password is required.",
            )
        if new_password != confirm:
            return _render_with_csrf(
                request, "admin_user_password.html",
                target_user=user, status_code=400,
                error="Passwords do not match.",
            )

        policy_err = await password_policy.validate_new_password(new_password)
        if policy_err:
            return _render_with_csrf(
                request, "admin_user_password.html",
                target_user=user, status_code=400, error=policy_err,
            )

        try:
            await auth_db.set_password(target, new_password)
        except ValueError as exc:
            return _render_with_csrf(
                request, "admin_user_password.html",
                target_user=user, status_code=400, error=str(exc),
            )

        # Always kill existing sessions after an admin-triggered reset.
        # The user must log in with the new password to get a fresh token.
        await auth_db.bump_token_generation(target)

        acting = request.state.user_id
        if isinstance(acting, str):
            acting = UUID(acting)
        await audit.log_event(
            "user.password_reset_by_admin",
            actor_user_id=acting,
            target_id=target,
            detail=audit.request_detail(request),
        )

        return _render_with_csrf(
            request, "admin_user_password.html",
            target_user=user, flash="Password reset. All existing sessions invalidated.",
        )

    @mcp.custom_route("/admin/users/{user_id}/offboard", methods=["POST"])
    @require_admin_csrf
    async def admin_users_offboard(request: Request):
        try:
            target = UUID(request.path_params["user_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid user id", status_code=400)

        acting = request.state.user_id
        if isinstance(acting, str):
            acting = UUID(acting)

        # Refuse to off-board yourself: it's almost always a mistake,
        # and a reset-admin + logout flow is the correct recovery.
        if target == acting:
            return HTMLResponse(
                "cannot off-board yourself; use --reset-admin instead",
                status_code=400,
            )

        removed = await auth_db.delete_user(target)
        if not removed:
            return HTMLResponse("user not found", status_code=404)

        await audit.log_event(
            "admin.offboard",
            actor_user_id=acting,
            target_id=target,
            detail=audit.request_detail(request),
        )
        return HTMLResponse("", status_code=200)

    # -- Admin: audit log --------------------------------------------------

    @mcp.custom_route("/admin/audit", methods=["GET"])
    @require_admin
    async def admin_audit_page(request: Request):
        params = request.query_params
        since_preset = _qs_str(params, "since") or ""
        filters = {
            "actor_username": _qs_str(params, "actor_username"),
            "event_type": _qs_str(params, "event_type"),
            "since": since_preset,
        }

        limit = max(1, min(_qs_int(params, "limit", 50), 200))
        offset = max(0, _qs_int(params, "offset", 0))

        events, total = await audit.list_events(
            offset=offset,
            limit=limit,
            actor_username=filters["actor_username"],
            event_type=filters["event_type"],
            since=audit.parse_since_preset(since_preset),
        )
        event_types = await audit.list_event_types()

        page = _pagination_ctx(
            offset=offset, limit=limit, total=total,
            filters=filters,
            filter_names=("actor_username", "event_type", "since"),
        )

        return _render_with_csrf(
            request, "admin_audit.html",
            events=events, count=len(events), total=total,
            offset=offset, limit=limit,
            filters=filters, event_types=event_types,
            **page,
        )

    # -- Admin: metrics dashboard ------------------------------------------

    @mcp.custom_route("/admin/metrics", methods=["GET"])
    @require_admin
    async def admin_metrics_page(request: Request):
        from lib import observability

        try:
            events_limit = max(1, min(int(
                request.query_params.get("events_limit", "50")
            ), observability._RECENT_EVENTS_CAPACITY))
        except (TypeError, ValueError):
            events_limit = 50

        snapshot = observability.metrics_snapshot()
        events = observability.recent_events(limit=events_limit)

        return _render_with_csrf(
            request, "admin_metrics.html",
            snapshot=snapshot,
            events=events,
            events_limit=events_limit,
            events_capacity=observability._RECENT_EVENTS_CAPACITY,
        )

    # -- Logout -----------------------------------------------------------

    @mcp.custom_route("/logout", methods=["POST"])
    async def logout_submit(request: Request):
        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        # Best-effort token revocation: find the cookie's token, resolve
        # it to a token row, mark revoked. If any step fails, we still
        # clear the cookie so the user is functionally logged out.
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        audit_actor: UUID | None = None
        audit_target: UUID | None = None
        if cookie:
            resolved = await auth_db.resolve_token(cookie)
            if resolved:
                try:
                    uid = (
                        UUID(resolved["user_id"])
                        if isinstance(resolved["user_id"], str)
                        else resolved["user_id"]
                    )
                    tid = UUID(resolved["token_id"])
                    audit_actor, audit_target = uid, tid
                    await auth_db.revoke_token(uid, tid)
                except (ValueError, TypeError) as exc:
                    # Token resolved but UUID conversion failed. Log at
                    # debug so we notice if tokens start landing here,
                    # but don't block logout — the cookie gets cleared
                    # regardless so the browser session is gone.
                    _log.debug(
                        "logout: could not revoke token cleanly (%s)", exc,
                    )

        await audit.log_event(
            "logout",
            actor_user_id=audit_actor,
            target_id=audit_target,
            detail=audit.request_detail(request),
        )

        resp = RedirectResponse("/login", status_code=303)
        _clear_session_cookie(resp)
        return resp

    # -- Memories list ----------------------------------------------------

    @mcp.custom_route("/memories", methods=["GET"])
    @require_login
    async def memories_page(request: Request):
        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        params = request.query_params
        filters = {
            "project": _qs_str(params, "project"),
            "scope": _qs_str(params, "scope"),
            "tag": _qs_str(params, "tag"),
            "min_importance": _qs_str(params, "min_importance"),
            "q": _qs_str(params, "q"),
            "sort": _qs_str(params, "sort") or "created_at",
            "order": _qs_str(params, "order") or "DESC",
        }
        limit = max(1, min(_qs_int(params, "limit", 20), 100))
        offset = max(0, _qs_int(params, "offset", 0))

        memories, total = await db.admin_list_memories(
            owner_user_id=uid,
            project=filters["project"],
            scope=filters["scope"],
            tag=filters["tag"],
            min_importance=filters["min_importance"],
            q=filters["q"],
            sort=filters["sort"],
            order=filters["order"],
            offset=offset,
            limit=limit,
        )

        page = _pagination_ctx(
            offset=offset, limit=limit, total=total,
            filters=filters,
            filter_names=("project", "scope", "tag", "min_importance",
                          "q", "sort", "order"),
        )

        # HTMX requests want just the list partial for in-place swap;
        # a full page reload gets the whole template.
        template = (
            "_memories_list.html"
            if request.headers.get("hx-request")
            else "memories.html"
        )

        return _render_with_csrf(
            request, template,
            memories=memories,
            count=len(memories),
            total=total,
            offset=offset,
            limit=limit,
            filters=filters,
            **page,
        )

    # -- Facts ------------------------------------------------------------

    @mcp.custom_route("/facts", methods=["GET"])
    @require_login
    async def facts_page(request: Request):
        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        params = request.query_params
        filters = {
            "subject": _qs_str(params, "subject"),
            "predicate": _qs_str(params, "predicate"),
            "scope": _qs_str(params, "scope"),
            "q": _qs_str(params, "q"),
            "include_history": params.get("include_history") in ("1", "on", "true"),
        }
        limit = max(1, min(_qs_int(params, "limit", 50), 200))
        offset = max(0, _qs_int(params, "offset", 0))

        facts, total = await db.admin_list_facts(
            owner_user_id=uid,
            subject=filters["subject"],
            predicate=filters["predicate"],
            scope=filters["scope"],
            q=filters["q"],
            include_history=filters["include_history"],
            offset=offset,
            limit=limit,
        )

        page = _pagination_ctx(
            offset=offset, limit=limit, total=total,
            filters=filters,
            filter_names=("subject", "predicate", "scope", "q"),
            extra_pairs=(("include_history", 1),) if filters["include_history"] else (),
        )

        template = (
            "_facts_list.html"
            if request.headers.get("hx-request")
            else "facts.html"
        )

        return _render_with_csrf(
            request, template,
            facts=facts,
            count=len(facts),
            total=total,
            offset=offset,
            limit=limit,
            filters=filters,
            **page,
        )

    @mcp.custom_route("/facts/{fact_id}", methods=["DELETE"])
    @require_login_csrf_xhr
    async def fact_delete(request: Request):
        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            fid = UUID(request.path_params["fact_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid id", status_code=400)

        ok = await db.forget_fact(fid, uid)
        if not ok:
            return HTMLResponse("not found", status_code=404)
        return HTMLResponse("", status_code=200)

    # -- Token management ------------------------------------------------

    @mcp.custom_route("/tokens", methods=["GET"])
    @require_login
    async def tokens_page(request: Request):
        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        tokens_list = await auth_db.list_tokens(uid)
        return _render_with_csrf(
            request, "tokens.html",
            tokens=tokens_list, count=len(tokens_list),
        )

    @mcp.custom_route("/tokens", methods=["POST"])
    @require_login_csrf
    async def tokens_create(request: Request):
        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        form = await request.form()
        label = (form.get("label") or "").strip() or None

        minted = await auth_db.create_token(uid, label=label)
        await audit.log_event(
            "token.mint",
            actor_user_id=uid,
            target_id=UUID(minted["id"]),
            detail={**audit.request_detail(request), "label": label or ""},
        )
        # Re-render the page with the raw value exposed as new_token.
        # The token is NOT stored server-side in this flow after this
        # render; if the user navigates away or reloads, the banner
        # disappears. Cookie-mint is idempotent on re-render.
        tokens_list = await auth_db.list_tokens(uid)
        return _render_with_csrf(
            request, "tokens.html",
            tokens=tokens_list, count=len(tokens_list),
            new_token=minted["token"],
            new_token_label=label,
        )

    @mcp.custom_route("/tokens/{token_id}", methods=["DELETE"])
    @require_login_csrf_xhr
    async def tokens_revoke(request: Request):
        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            token_uuid = UUID(request.path_params["token_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid token id", status_code=400)

        ok = await auth_db.revoke_token(uid, token_uuid)
        if not ok:
            return HTMLResponse("not found", status_code=404)
        await audit.log_event(
            "token.revoke",
            actor_user_id=uid,
            target_id=token_uuid,
            detail=audit.request_detail(request),
        )

        # HTMX swap target is the row itself. Return empty body so the
        # row vanishes, matching how memory delete works. Alternative
        # would be returning an updated row showing the revoked state,
        # but that requires a partial-render handler; vanishing is
        # simpler and the full list is one click away.
        return HTMLResponse("", status_code=200)

    @mcp.custom_route("/memories/bulk-delete", methods=["POST"])
    @require_login_csrf
    async def memories_bulk_delete(request: Request):
        """
        Delete a set of selected memories in one call. The form posts
        `ids` once per selected checkbox; we collect them all, parse
        to UUIDs, and delete owner-scoped. Returns the refreshed
        /memories panel on HX-Request (HTMX), or redirects otherwise.
        """
        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        form = await request.form()
        # getlist is the multi-value accessor in Starlette's FormData.
        raw_ids = form.getlist("ids")
        if len(raw_ids) > _BULK_DELETE_LIMIT:
            _log.debug(
                "bulk_delete: truncating %d ids to cap %d",
                len(raw_ids), _BULK_DELETE_LIMIT,
            )
            raw_ids = raw_ids[:_BULK_DELETE_LIMIT]

        valid_ids = []
        for raw in raw_ids:
            try:
                valid_ids.append(UUID(raw))
            except (ValueError, TypeError) as exc:
                _log.debug(
                    "bulk_delete: skipping unparseable id %r (%s)",
                    raw, exc,
                )

        deleted = await db.admin_bulk_delete(valid_ids, uid)

        # Refresh the list with any current filters preserved. The
        # bulk form posts WITHOUT the filter values (to keep the
        # form simple); reconstruct from the referrer's query string
        # or fall back to the bare /memories.
        #
        # Easy path: if HTMX, use the full-reload redirect via HX-Redirect
        # header so the client hits /memories?{filters} naturally.
        if request.headers.get("hx-request"):
            return HTMLResponse(
                "",
                status_code=200,
                headers={"HX-Redirect": "/memories"},
            )

        return RedirectResponse(
            f"/memories?flash=Deleted+{deleted}+memor{'y' if deleted == 1 else 'ies'}",
            status_code=303,
        )

    @mcp.custom_route("/memories/{memory_id}", methods=["GET"])
    @require_login
    async def memory_detail(request: Request):
        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            mid = UUID(request.path_params["memory_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid id", status_code=400)

        memory = await db.admin_get_memory(mid, uid)
        if memory is None:
            return HTMLResponse("not found", status_code=404)

        return _render_with_csrf(
            request, "memory_detail.html",
            memory=memory,
        )

    @mcp.custom_route("/memories/{memory_id}", methods=["POST"])
    @require_login_csrf
    async def memory_update(request: Request):
        """
        Edit a memory. POST rather than PATCH so the HTML form can
        submit it directly without needing a JS shim. The route
        accepts content, tags, importance, and project; any subset
        of those fields gets updated. Content change triggers a
        re-embed.
        """
        from lib import db
        from lib.embeddings import embed

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            mid = UUID(request.path_params["memory_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid id", status_code=400)

        # Fetch existing so we can diff and leave un-submitted fields
        # alone (HTML forms send "" for empty inputs; we need to
        # distinguish "cleared by user" from "same as before").
        existing = await db.admin_get_memory(mid, uid)
        if existing is None:
            return HTMLResponse("not found", status_code=404)

        form = await request.form()
        errors = []

        new_content = form.get("content")
        new_tags_raw = form.get("tags")
        new_importance = form.get("importance")
        new_project = (form.get("project") or "").strip()

        updates: dict = {}

        if new_content is not None and new_content != existing["content"]:
            if not new_content.strip():
                errors.append("Content cannot be empty.")
            else:
                updates["content"] = new_content
                updates["embedding"] = embed(new_content)

        if new_tags_raw is not None:
            # comma-separated list in the form; strip + dedupe order
            parsed = [t.strip() for t in new_tags_raw.split(",") if t.strip()]
            if parsed != existing["tags"]:
                updates["tags"] = parsed

        if new_importance and new_importance != existing["importance"]:
            if new_importance not in ("low", "normal", "high", "critical"):
                errors.append(f"Invalid importance: {new_importance}")
            else:
                updates["importance"] = new_importance

        if new_project and new_project != existing.get("project_directory"):
            # Normalize + validate the new scope. Same rules as
            # memory_store: reject bare names / relative paths.
            from server import _normalize_project, _validate_writable_scope
            normalized = _normalize_project(new_project)
            scope_err = _validate_writable_scope(normalized)
            if scope_err:
                errors.append(scope_err)
            else:
                new_project_id = await db.get_or_create_project(
                    normalized, uid,
                )
                updates["project_id"] = new_project_id

        if errors:
            return _render_with_csrf(
                request, "memory_detail.html",
                memory=existing,
                errors=errors,
                status_code=400,
            )

        if not updates:
            return _render_with_csrf(
                request, "memory_detail.html",
                memory=existing,
                flash="No changes.",
            )

        await db.admin_update_memory(mid, uid, **updates)

        # Re-fetch so the rendered detail reflects the new state.
        refreshed = await db.admin_get_memory(mid, uid)
        return _render_with_csrf(
            request, "memory_detail.html",
            memory=refreshed,
            flash="Saved.",
        )

    @mcp.custom_route("/memories/{memory_id}", methods=["DELETE"])
    @require_login_csrf_xhr
    async def memory_delete(request: Request):
        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        try:
            mid = UUID(request.path_params["memory_id"])
        except (ValueError, KeyError):
            return HTMLResponse("invalid id", status_code=400)

        deleted = await db.forget_memory(mid, uid)
        if not deleted:
            return HTMLResponse("not found", status_code=404)

        # Empty body tells HTMX to remove the swap target (outerHTML
        # replaces with nothing). HTMX treats 200 + empty as the row
        # vanishing, which is exactly what we want.
        return HTMLResponse("", status_code=200)
