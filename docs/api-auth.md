# Auth API

Bearer-token auth for the MCP server. Shipped on `feature/mcp-hardening`
(2026-04-19). First `POST /auth/register` on a fresh server becomes
admin and adopts every pre-auth memory, project, and fact.

All endpoints return JSON. Errors look like `{"error": "..."}` with an
HTTP 4xx status.

## Base URL

Whatever your MCP URL is, minus `/mcp`. Examples:

```
http://memory.example.com:9500   # remote deployment
http://localhost:9500            # local server
```

## Authentication model

- **Password** hashes with `hashlib.scrypt` (salted, cost params
  stored with the digest so they can be raised later without
  re-hashing).
- **Tokens** look like `nnm_<43 urlsafe chars>` (256 bits of entropy).
  The raw value is shown exactly once at creation; the database only
  ever sees the scrypt hash.
- Every protected route expects `Authorization: Bearer nnm_...`.
- Revocation is immediate (partial index on `revoked_at IS NULL`).

## Localhost bypass

Set `MEMORY_AUTH_LOCALHOST_BYPASS=1` in the server's environment to
let loopback callers act as admin without a token. Intended for the
single-user personal-scope case where the server binds to 127.0.0.1
only. When the server binds to a non-loopback interface (the default
for any shared deployment), leave the env var unset so every call
must present a token.

## Endpoints

### `POST /auth/register`

Create a user. First call on a fresh server bootstraps an admin and
adopts pre-auth rows. Subsequent calls require an existing admin's
Bearer token.

```bash
# First user (no auth required)
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
    "is_admin": true,
    "created_at": "2026-04-19T..."
  },
  "adopted": {"projects": 1, "memories": 1, "facts": 1},
  "bootstrap_admin": true
}
```

```bash
# Admin adds another user
curl -X POST http://memory.example.com:9500/auth/register \
  -H 'Authorization: Bearer nnm_ADMIN_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"..."}'
```

Response (201): same shape without `adopted` / `bootstrap_admin`.

Errors:
- 400 if username/password missing or password under 8 chars
- 403 if a user already exists and the caller isn't admin
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
  "user": {"id":"...","username":"alice","is_admin":true},
  "token": {
    "id": "...",
    "user_id": "...",
    "label": "laptop",
    "created_at": "2026-04-19T...",
    "token": "nnm_1Nuitm8AfhcNNjXx52QubU5KT-3dl3KS6TvkZ2T-e-0"
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
  "is_admin": true,
  "localhost_bypass": false
}
```

With the bypass enabled and no token sent from loopback:
```json
{"user_id": null, "username": null, "is_admin": true, "localhost_bypass": true}
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

### Hooks

`claude/hooks/hooks.env` and `nnc/hooks/hooks.env` only carry
`MEMORY_MCP_URL` today. When the server requires auth, extend
`merge_hooks.py::_write_hooks_env` to also write
`MEMORY_MCP_TOKEN=nnm_...` and update the three hook scripts to set
`Authorization: Bearer <token>` on their `urllib.request.Request`
calls. Every hook file has its own HTTP call, so the change is
three one-liners per client (six total across `claude/` and `nnc/`).

## Rate limiting

Not implemented. The scrypt cost on token verification (~100ms per
miss) provides a soft rate limit against token-guessing attacks.
For brute-force login attempts against known usernames, consider a
reverse proxy (nginx, caddy) in front of `/auth/login` or add
per-IP throttling to the middleware.

## Rolling out to an existing server

1. Deploy the new code.
2. Migration `005_auth_users.sql` runs on first tool call. Existing
   memories stay accessible because `owner_user_id` is nullable and
   no query filters on it yet.
3. Hit `POST /auth/register` once, as yourself, to become admin.
   Pre-existing memories get adopted in the same call.
4. Update your Claude Code / LM Studio config to send a Bearer
   header, using the token returned by `/auth/login`.
5. Keep `MEMORY_AUTH_LOCALHOST_BYPASS` unset on any non-loopback
   deployment.

## Rolling back

Apply `config/migrations/rollback/005_auth_users.sql` manually
(inside a transaction). Drops users, auth_tokens, and the
`owner_user_id` columns. Memory and fact rows survive with ownership
links removed.
