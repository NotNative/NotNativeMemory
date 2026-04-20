# Security

This document captures the security posture of NotNativeMemory (NNM):
what it defends against, how it expects to be deployed, and how to
report issues. Operators should read it before exposing the server
outside loopback.

## Threat model

NNM is designed for **small-team / personal deployments** — a handful
of users running agents on their own workstations, optionally with a
shared server accessible over the LAN or internet behind a reverse
proxy. It is *not* designed as a multi-tenant SaaS.

**In scope:**

- Unauthorized cross-user access: user A should never see user B's
  memories, facts, projects, or token list.
- Credential compromise via brute force, credential stuffing, or
  breached-password reuse.
- Session hijacking through CSRF, cookie theft on plaintext HTTP,
  or stolen Bearer tokens.
- Input-shape abuse: oversized bodies, oversized fields, malformed
  JSON, injection into SQL / HTML / response headers.
- Username enumeration via response timing.
- Forensic recovery after suspected incidents.

**Out of scope:**

- Multi-tenant isolation at SaaS scale (no row-level-security activation
  by default; see `docs/planning/` for the runbook to turn it on with a
  dedicated DB role).
- Defense against adversaries who already have code execution on the
  MCP host (they read `.env`, read the DB, read the process memory).
- Denial of service beyond the basic safeguards (request body cap,
  rate-limited login, bounded bulk-delete). A motivated attacker can
  exhaust CPU / disk / DB connections.
- Cryptographic agility (the project stores `scrypt$N$r$p$salt$hash`
  strings so cost parameters can be raised without rehashing existing
  users, but swapping algorithms requires code + migration work).

## Controls summary

| Control | Where |
|---|---|
| Password hashing | scrypt with per-row salt; `lib/auth.py` |
| Token storage | split-format (plain lookup_key + scrypt secret); `lib/auth_db.py` |
| CSRF | double-submit cookie, `SameSite=Lax`; `lib/csrf.py` |
| Session revocation | `users.token_generation` counter; `lib/auth_db.py::bump_token_generation` |
| Rate limiting | exponential backoff per IP + per username; `lib/rate_limit.py` |
| Username enumeration | constant-time verify via dummy hash; `lib/auth.py::verify_or_dummy` |
| Request size | global 10 MiB cap + per-field caps; `lib/limits.py` |
| Response hardening | CSP + X-Content-Type-Options + Referrer-Policy + Permissions-Policy; `lib/security_headers.py` |
| Breach-list check | HIBP k-anonymity at registration; `lib/password_policy.py` |
| Audit trail | append-only `audit_events` table; `lib/audit.py` |
| SQL safety | 100% parameterized queries via asyncpg |
| XSS defense | Jinja2 autoescape, no `\|safe` usage |
| Per-user isolation | `owner_user_id` filter on every user-scoped read/write; RLS policies defined, inert until a non-superuser role is configured |

## Deployment shapes

See `docs/README.md` → "Deployment Shapes" for the full matrix. The
two env vars that gate network posture are:

- `MEMORY_BIND_HOST` — which interface uvicorn listens on. Default
  `0.0.0.0`. Set to `127.0.0.1` for loopback-only.
- `MEMORY_COOKIE_SECURE` — when set to `1`, session and CSRF cookies
  carry the `Secure` attribute. Required when binding a non-loopback
  interface.

Binding to a non-loopback interface without `MEMORY_COOKIE_SECURE=1`
prints a loud warning at startup and keeps running. That state is
**not recommended** beyond throwaway LAN dev.

Standard production shape: loopback bind on the application, TLS
terminated at a reverse proxy (nginx / Caddy / Traefik), proxy adds
`Strict-Transport-Security`, `MEMORY_COOKIE_SECURE=1` set so cookies
only flow over TLS.

## Reporting a vulnerability

**Do not file public GitHub issues for security bugs.**

Email the maintainer at the address in the repository's git history
(see `git log --format="%an %ae" | head -1`). Include:

- A short description of the issue.
- The affected commit SHA or release tag.
- Reproduction steps (proof-of-concept is welcome but not required).
- Your disclosure timeline expectation.

The maintainer will acknowledge within 7 days and aim to ship a fix
within 30 days for high-severity issues. Low-severity reports may be
folded into a regular release.

## Known limitations

- **RLS is scaffolded, not activated.** See
  `docs/planning/security-phased-plan.md` §3.1 for the activation
  runbook. Current per-user isolation relies on explicit
  `owner_user_id` filters in `lib/db.py`; Phase 7 tests (`tests/`)
  verify no cross-user path.
- **No admin role yet.** All users are peers. The admin-bootstrap /
  first-user design is captured in session history and pending
  implementation. Until it lands, "force log out a user" must be
  done via SQL (`UPDATE users SET token_generation =
  token_generation + 1 WHERE id = ...`).
- **Localhost bypass is still available** via
  `MEMORY_AUTH_LOCALHOST_BYPASS=1` + `MEMORY_AUTH_LOCALHOST_USER=...`.
  It is explicitly gated by both env vars and only fires on loopback
  traffic, but reviewers should be aware it exists. Disable in
  shared / multi-user deployments.
