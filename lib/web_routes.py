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

import logging
import os
from uuid import UUID

import asyncpg
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from lib import auth, auth_db, rate_limit
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
        "request": request,
        "username": getattr(request.state, "username", None),
        "user_id": getattr(request.state, "user_id", None),
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
        template, ctx, status_code=status_code,
    )
    if is_new:
        set_csrf_cookie(response, token)
    return response


def _require_login(request: Request) -> RedirectResponse | None:
    """Return a redirect response if not logged in, else None."""
    if not getattr(request.state, "user_id", None):
        return RedirectResponse("/login", status_code=302)
    return None


# -- Routes -----------------------------------------------------------------


def register_routes(mcp) -> None:
    """Attach all HTML / form routes to the FastMCP instance."""

    @mcp.custom_route("/", methods=["GET"])
    async def root(request: Request):
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
            wait = max(1, int(retry + 0.999))
            resp = _render_with_csrf(
                request, "login.html",
                status_code=429,
                error=f"Too many login attempts. Try again in {wait}s.",
            )
            resp.headers["Retry-After"] = str(wait)
            return resp

        record = await auth_db.get_user_by_username(username)
        if record is None or not auth.verify_secret(password, record["password_hash"]):
            rate_limit.record_login_failure(ip, username)
            return _render_with_csrf(
                request, "login.html",
                status_code=401,
                error="Invalid username or password.",
            )

        rate_limit.clear_login(ip, username)
        token = await auth_db.create_token(record["id"], label="web-session")
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

        try:
            await auth_db.create_user(username, password)
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

        # Redirect to login rather than auto-login: forces the user to
        # confirm they remember the password they just typed.
        return RedirectResponse("/login", status_code=303)

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
        if cookie:
            resolved = await auth_db.resolve_token(cookie)
            if resolved:
                try:
                    await auth_db.revoke_token(
                        UUID(resolved["user_id"])
                        if isinstance(resolved["user_id"], str)
                        else resolved["user_id"],
                        UUID(resolved["token_id"]),
                    )
                except (ValueError, TypeError) as exc:
                    # Token resolved but UUID conversion failed. Log at
                    # debug so we notice if tokens start landing here,
                    # but don't block logout — the cookie gets cleared
                    # regardless so the browser session is gone.
                    _log.debug(
                        "logout: could not revoke token cleanly (%s)", exc,
                    )

        resp = RedirectResponse("/login", status_code=303)
        _clear_session_cookie(resp)
        return resp

    # -- Memories list ----------------------------------------------------

    @mcp.custom_route("/memories", methods=["GET"])
    async def memories_page(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        params = request.query_params

        def _str(name: str) -> str | None:
            value = params.get(name, "").strip()
            return value or None

        def _int(name: str, default: int) -> int:
            raw = params.get(name, "")
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        filters = {
            "project": _str("project"),
            "scope": _str("scope"),
            "tag": _str("tag"),
            "min_importance": _str("min_importance"),
            "q": _str("q"),
            "sort": _str("sort") or "created_at",
            "order": _str("order") or "DESC",
        }
        limit = max(1, min(_int("limit", 20), 100))
        offset = max(0, _int("offset", 0))

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

        # Build current query string minus offset (for pagination links
        # to rebuild it themselves). Prev/next just rewrite `offset`.
        base_qs = []
        for name in ("project", "scope", "tag", "min_importance",
                     "q", "sort", "order"):
            value = filters.get(name)
            if value:
                base_qs.append(f"{name}={value}")
        base_qs.append(f"limit={limit}")
        qs_without_offset = "&".join(base_qs)

        prev_offset = max(0, offset - limit) if offset > 0 else None
        next_offset = offset + limit if offset + limit < total else None

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
            qs_without_offset=qs_without_offset,
            prev_offset=prev_offset,
            next_offset=next_offset,
        )

    # -- Facts ------------------------------------------------------------

    @mcp.custom_route("/facts", methods=["GET"])
    async def facts_page(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        from lib import db

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        params = request.query_params

        def _str(name: str):
            v = params.get(name, "").strip()
            return v or None

        def _int(name: str, default: int) -> int:
            try:
                return int(params.get(name, ""))
            except (TypeError, ValueError):
                return default

        filters = {
            "subject": _str("subject"),
            "predicate": _str("predicate"),
            "scope": _str("scope"),
            "q": _str("q"),
            "include_history": params.get("include_history") in ("1", "on", "true"),
        }
        limit = max(1, min(_int("limit", 50), 200))
        offset = max(0, _int("offset", 0))

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

        base_qs = []
        for name in ("subject", "predicate", "scope", "q"):
            v = filters.get(name)
            if v:
                base_qs.append(f"{name}={v}")
        if filters["include_history"]:
            base_qs.append("include_history=1")
        base_qs.append(f"limit={limit}")
        qs_without_offset = "&".join(base_qs)

        prev_offset = max(0, offset - limit) if offset > 0 else None
        next_offset = offset + limit if offset + limit < total else None

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
            qs_without_offset=qs_without_offset,
            prev_offset=prev_offset,
            next_offset=next_offset,
        )

    @mcp.custom_route("/facts/{fact_id}", methods=["DELETE"])
    async def fact_delete(request: Request):
        redirect = _require_login(request)
        if redirect:
            return HTMLResponse("unauthorized", status_code=401)

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

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
    async def tokens_page(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        tokens_list = await auth_db.list_tokens(uid)
        return _render_with_csrf(
            request, "tokens.html",
            tokens=tokens_list, count=len(tokens_list),
        )

    @mcp.custom_route("/tokens", methods=["POST"])
    async def tokens_create(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

        uid = request.state.user_id
        if isinstance(uid, str):
            uid = UUID(uid)

        form = await request.form()
        label = (form.get("label") or "").strip() or None

        minted = await auth_db.create_token(uid, label=label)
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
    async def tokens_revoke(request: Request):
        redirect = _require_login(request)
        if redirect:
            return HTMLResponse("unauthorized", status_code=401)

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

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

        # HTMX swap target is the row itself. Return empty body so the
        # row vanishes, matching how memory delete works. Alternative
        # would be returning an updated row showing the revoked state,
        # but that requires a partial-render handler; vanishing is
        # simpler and the full list is one click away.
        return HTMLResponse("", status_code=200)

    @mcp.custom_route("/memories/bulk-delete", methods=["POST"])
    async def memories_bulk_delete(request: Request):
        """
        Delete a set of selected memories in one call. The form posts
        `ids` once per selected checkbox; we collect them all, parse
        to UUIDs, and delete owner-scoped. Returns the refreshed
        /memories panel on HX-Request (HTMX), or redirects otherwise.
        """
        redirect = _require_login(request)
        if redirect:
            return redirect

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

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
    async def memory_detail(request: Request):
        redirect = _require_login(request)
        if redirect:
            return redirect

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
    async def memory_update(request: Request):
        """
        Edit a memory. POST rather than PATCH so the HTML form can
        submit it directly without needing a JS shim. The route
        accepts content, tags, importance, and project; any subset
        of those fields gets updated. Content change triggers a
        re-embed.
        """
        redirect = _require_login(request)
        if redirect:
            return redirect

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

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
    async def memory_delete(request: Request):
        redirect = _require_login(request)
        if redirect:
            # HTMX DELETE to an unauthed endpoint: return 401 and let
            # the client decide. Redirects wouldn't be followed by HTMX.
            return HTMLResponse("unauthorized", status_code=401)

        csrf_err = await check_csrf(request)
        if csrf_err is not None:
            return csrf_err

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
