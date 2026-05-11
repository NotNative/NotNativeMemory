# Auth API

Bearer-token auth for the MCP server. Open self-registration; every
user sees only their own memories. No admin concept.

All endpoints return JSON. Errors look like `{"error": "..."}` with an
HTTP 4xx status.

## Base URL

Whatever your MCP URL is, minus `/mcp`. Examples:

```
http://memory.example.com:9500   # remote deployment
http://localhost:9500            # local server
```

## Operational modes

The server runs in one of two modes, chosen at install time.

**Solo mode** (default for single-user personal use):
- `MEMORY_AUTH_LOCALHOST_BYPASS=1`
- `MEMORY_AUTH_LOCALHOST_USER=<username>`
- Loopback requests without an `Authorization` header are
  authenticated as the named user. This is what lets hooks and
  on-host agents reach the server with no token.
- An explicit Bearer header ALWAYS wins: a request from loopback
  that carries a token is authenticated by that token, not the
  bypass user.

**Multi-user mode** (deployments with more than one person):
- `MEMORY_AUTH_LOCALHOST_BYPASS=0` (or unset)
- Every caller must present a valid Bearer token. No bypass.

Switching solo to multi is a config flip plus a server restart; no
data migration needed because solo-mode writes are already tagged
with a real `owner_user_id`.

## Authentication model

- **Password** hashes with `hashlib.scrypt` (salted, cost params
  stored with the digest so they can be raised later without
  re-hashing). Minimum 8 characters.
- **Tokens** look like `nnm_<43 urlsafe chars>` (256 bits of entropy).
  The raw value is shown exactly once at creation; the database only
  ever sees the scrypt hash.
- Every protected route expects `Authorization: Bearer nnm_...`.
- Revocation is immediate (partial index on `revoked_at IS NULL`).
- Each user is isolated: `_global` for user A is a different row than
  `_global` for user B. Same for domain scopes and local projects.

## Endpoints

### `POST /auth/register`

Open self-registration. Anyone can create a user; the account has
no special privileges and only sees memories it writes itself.

```bash
curl -X POST http://memory.example.com:9500/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"at-least-8-chars"}'
```

Response (201):
```json
{
  "user": {
    "id": "...",
    "username": "alice",
    "created_at": "2026-04-19T..."
  }
}
```

Errors:
- 400 if username/password missing or password under 8 chars
- 409 if username is taken

### `POST /auth/login`

Exchange password for a new Bearer token. The raw token is returned
once in `token.token`. Save it; the server will not show it again.

```bash
curl -X POST http://memory.example.com:9500/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"...","label":"laptop"}'
```

Response (200):
```json
{
  "user": {"id":"...","username":"alice"},
  "token": {
    "id": "...",
    "user_id": "...",
    "label": "laptop",
    "created_at": "2026-04-19T...",
    "token": "nnm_EXAMPLE_TOKEN_SHOWN_ONCE_AT_CREATION"
  }
}
```

`label` is optional; use it to remember where the token lives. Every
login mints a new token (does not reuse an existing one), so over
time you accumulate tokens per device. Revoke the old ones when you
rotate.

Errors:
- 400 body not JSON
- 401 invalid credentials (intentionally generic, no username probing)

### `GET /auth/tokens`

List your own tokens. Returns metadata only, never the raw value or
the hash.

```bash
curl -H "Authorization: Bearer nnm_..." \
  http://memory.example.com:9500/auth/tokens
```

Response (200):
```json
{
  "tokens": [
    {
      "id": "...",
      "label": "laptop",
      "created_at": "...",
      "last_used_at": "...",
      "revoked_at": null
    }
  ],
  "count": 1
}
```

`last_used_at` updates on every successful request that uses the
token. `revoked_at` is non-null for revoked tokens (they stay in the
list so you can see history).

### `POST /auth/tokens`

Mint a new token for yourself. Useful for adding a new device
without having to re-enter your password.

```bash
curl -X POST \
  -H "Authorization: Bearer nnm_..." \
  -H 'Content-Type: application/json' \
  -d '{"label":"workstation"}' \
  http://memory.example.com:9500/auth/tokens
```

Response (201): same shape as `login`'s `token` object.

### `DELETE /auth/tokens/{id}`

Revoke one of your own tokens. Only works on tokens that belong to
you; revoking someone else's token returns 404 with no side effects.

```bash
curl -X DELETE \
  -H "Authorization: Bearer nnm_..." \
  http://memory.example.com:9500/auth/tokens/TOKEN-UUID
```

Response (200):
```json
{"revoked": true}
```

Errors:
- 400 invalid UUID in path
- 404 token doesn't exist, isn't yours, or is already revoked

### `GET /auth/me`

Echo the authenticated identity. Handy for testing tokens and for
confirming the localhost bypass is active.

```bash
curl -H "Authorization: Bearer nnm_..." http://memory.example.com:9500/auth/me
```

Response (200):
```json
{
  "user_id": "...",
  "username": "alice",
  "localhost_bypass": false
}
```

From loopback in solo mode without a token:
```json
{"user_id": "...", "username": "<solo-user>", "localhost_bypass": true}
```

### `GET /health`

Public, no auth. Returns `{"status": "ok"}` if the HTTP layer is up.
Does not check the database. Useful for liveness probes.

## MCP client setup

### Claude Code

```bash
claude mcp remove memory
claude mcp add --transport http memory --scope user \
  --header "Authorization: Bearer nnm_YOUR_TOKEN" \
  http://memory.example.com:9500/mcp
```

### LM Studio

`~/.lmstudio/mcp.json`:

```json
{
  "memory": {
    "type": "http",
    "url": "http://memory.example.com:9500/mcp",
    "headers": {
      "Authorization": "Bearer nnm_YOUR_TOKEN"
    }
  }
}
```

### NotNativeAgent

`~/.nna/settings.json`:

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "http://memory.example.com:9500/mcp",
      "headers": {
        "Authorization": "Bearer nnm_YOUR_TOKEN"
      }
    }
  }
}
```

### Hooks

`hook_bundles/claude/notnative-memory/hooks.env` and `hook_bundles/nna/notnative-memory/hooks.env` only carry
`MEMORY_MCP_URL` today. In solo mode the hooks work without a token
because bypass handles loopback. In multi-user mode, extend
`merge_hooks.py::_write_hooks_env` to also write
`MEMORY_MCP_TOKEN=nnm_...` and update the hook scripts to set
`Authorization: Bearer <token>` on their `urllib.request.Request`
calls. Every hook file has its own HTTP call, so the change is
one-liners per client across `hook_bundles/claude/notnative-memory/` and `hook_bundles/nna/notnative-memory/`.

## Rate limiting

Not implemented. The scrypt cost on token verification (~100ms per
miss) provides a soft rate limit against token-guessing attacks.
For brute-force login attempts against known usernames, consider a
reverse proxy (nginx, caddy) in front of `/auth/login` or add
per-IP throttling to the middleware.

## Rolling out to an existing server

Fresh install:
1. Run the installer, pick solo or multi-user mode.
2. Solo: installer creates a single user, writes their name to
   `.env` as `MEMORY_AUTH_LOCALHOST_USER`. No manual steps.
3. Multi-user: start the server, each user registers themselves
   via `POST /auth/register` and logs in for a token.

Upgrade from a pre-auth install:
1. Deploy the new code.
2. Migrations 005 and 006 run on first tool call. 006 needs no
   orphan rows: either you already have a Phase 5 admin (orphans
   were adopted at registration time) or you have exactly one
   user post-005 (006 auto-adopts orphans to that user).
3. If the migration halts with an orphan-count error, either
   register a single user first or assign orphans manually, then
   restart the server.

## Rolling back

Apply `config/migrations/rollback/006_per_user_scoping.sql` then
`005_auth_users.sql` manually (inside transactions). Rollback 006
fails if two users share a directory value; merge or delete those
rows first. Rollback 005 drops users, auth_tokens, and the
`owner_user_id` columns entirely.
