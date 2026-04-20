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

import os
from uuid import UUID

import asyncpg
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from lib import auth, auth_db
from lib.csrf import ensure_csrf, check_csrf


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


def _context_for(request: Request, **extra) -> dict:
    """Base template context. Adds the authenticated identity so nav renders.

    CSRF token is not populated here because the cookie is set on the
    response, which the view handler hasn't built yet. View handlers
    that render a form call `_render_with_csrf` instead.
    """
    ctx = {
        "request": request,
        "username": getattr(request.state, "username", None),
        "user_id": getattr(request.state, "user_id", None),
        "csrf_token": request.cookies.get("nnm_csrf", ""),
    }
    ctx.update(extra)
    return ctx


def _render_with_csrf(
    request: Request, template: str, status_code: int = 200, **extra,
):
    """
    Render a template with a guaranteed-present csrf_token in context.
    Mints the cookie onto the response if missing.
    """
    ctx = _context_for(request, **extra)
    response = templates.TemplateResponse(
        template, ctx, status_code=status_code,
    )
    token = ensure_csrf(request, response)
    # Jinja already rendered with whatever was in the cookie at read
    # time. If we just minted a fresh token, rewrite the rendered body
    # so the form carries the new value that the browser is about to
    # receive.
    if not ctx["csrf_token"]:
        ctx["csrf_token"] = token
        response = templates.TemplateResponse(
            template, ctx, status_code=status_code,
        )
        ensure_csrf(request, response)
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

        record = await auth_db.get_user_by_username(username)
        if record is None or not auth.verify_secret(password, record["password_hash"]):
            return _render_with_csrf(
                request, "login.html",
                status_code=401,
                error="Invalid username or password.",
            )

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
                except (ValueError, TypeError):
                    pass

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
