# Incident Response Playbook

Concrete recipes for the handful of security-incident shapes NNM
operators will realistically face. Each entry lists symptoms, the
immediate action, and the follow-up audit.

Run every destructive SQL statement below inside a transaction
(`BEGIN; ... COMMIT;`) until you have verified the row count you
expected. Stop and investigate if the count is wrong; do not
COMMIT a surprise.

## Force-logout a specific user

**Symptom:** suspected session hijack (user's token leaked, user
reports sudden logins they did not make, etc.).

**Action:** bump the user's token_generation counter. Every
outstanding token for that user becomes stale on the next auth
attempt.

```sql
BEGIN;
UPDATE users
SET token_generation = token_generation + 1
WHERE username = '<the_user>';
-- verify exactly 1 row
SELECT username, token_generation FROM users WHERE username = '<the_user>';
COMMIT;
```

The user will be prompted to log in again on their next request.
Their password is unchanged; if you suspect the password is also
compromised, force a password change separately (see below).

**Follow-up:** inspect `audit_events` for the user over the window
of concern.

```sql
SELECT at, event_type, detail
FROM audit_events
WHERE actor_user_id = (SELECT id FROM users WHERE username = '<the_user>')
  AND at > now() - interval '7 days'
ORDER BY at DESC;
```

## Force-logout everyone (suspected server-side breach)

**Symptom:** unexplained rows in the DB, evidence of filesystem
tampering, reverse-proxy logs showing unexpected traffic, or out-of-
band intelligence that the server was compromised.

**Action:** bump every user's token_generation in one statement.

```sql
BEGIN;
UPDATE users SET token_generation = token_generation + 1;
-- verify the row count matches the expected user count
SELECT COUNT(*) FROM users;
COMMIT;
```

Every outstanding token across the system becomes stale immediately.

**Follow-up:**
1. Rotate the DB password (`MEMORY_DB_PASSWORD`) and restart the
   server.
2. Rotate the TLS cert on the reverse proxy if a key compromise is
   possible.
3. Inspect `audit_events` for anomalies in the relevant window.
4. Decide whether to rotate the embedding model weights (probably no
   — they are public artifacts — but verify the checksum against
   upstream if you suspect tampering).

## Rotate a user's password

**Symptom:** user reports password compromise, password appears in a
breach feed, or a force-logout-user is not sufficient confidence.

**Action:** there is no self-service password reset yet. Reset via
the CLI helper on the server host:

```bash
python server.py --create-user '<the_user>'
```

This prompts for a new password and updates the row if the user
already exists. Follow with the token-generation bump above so
every session tied to the old credentials is killed.

## Off-board a user

**Symptom:** employee left, account no longer needed, or account is
compromised beyond recovery.

**Action:** delete the user row. Every memory, fact, project, and
token they owned cascades.

```sql
BEGIN;
-- sanity-check what you're about to delete
SELECT
  (SELECT COUNT(*) FROM memories WHERE owner_user_id = u.id) AS memories,
  (SELECT COUNT(*) FROM facts WHERE owner_user_id = u.id) AS facts,
  (SELECT COUNT(*) FROM projects WHERE owner_user_id = u.id) AS projects,
  (SELECT COUNT(*) FROM auth_tokens WHERE user_id = u.id) AS tokens
FROM users u WHERE username = '<the_user>';

DELETE FROM users WHERE username = '<the_user>';
COMMIT;
```

`audit_events` rows for the deleted user survive with
`actor_user_id` set to NULL (ON DELETE SET NULL). Historical
forensic trail is preserved; the identity link is gone.

## Suspected credential-stuffing campaign

**Symptom:** sustained burst of `login.fail` events, usually from a
handful of IPs or against a handful of usernames.

**Action:**
1. Check `audit_events` for the pattern.

   ```sql
   SELECT detail->>'ip' AS ip, COUNT(*) AS attempts
   FROM audit_events
   WHERE event_type = 'login.fail'
     AND at > now() - interval '1 hour'
   GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
   ```

2. Rate limiting is already slowing them (exponential backoff per
   IP + per username, capped at 5 min). If the source is small and
   predictable, block at the reverse-proxy or firewall layer.

3. If specific usernames are being targeted, consider bumping their
   `token_generation` prophylactically so that *if* the attacker
   gets in during the window, their session is immediately killed
   when you notice.

## Rotate TLS certificate

**Symptom:** cert nearing expiry, or suspected private key
compromise.

**Action:** this is reverse-proxy work, not NNM work. Follow your
proxy's documented renewal path (Let's Encrypt `certbot renew`,
etc.). NNM itself only sees plain HTTP from the proxy.

If you suspect the proxy's private key was exposed, also
force-logout everyone (above), because any cookies observed by an
attacker during the compromise window are replayable until their
session generation rolls over.

## Reset the admin user (pending implementation)

*Admin role is not yet implemented.* When it lands (per the
admin-bootstrap design discussed in-session), the recovery path
is expected to be:

1. Stop the server.
2. Run `python -m nnm reset-admin` to clear the admin flag on every
   user and regenerate the bootstrap token file.
3. Restart the server.
4. Browse to the web GUI, claim root via the new token.

This section will be updated when the admin feature ships.

## What NOT to do

- **Do not TRUNCATE any table as a debugging shortcut.** The
  `tests/test_owner_propagation.py` comment captures why: a previous
  version TRUNCATE'd `users CASCADE` and wiped every user's memories,
  facts, tokens, and projects.

- **Do not delete from `audit_events` after an incident** unless
  you are intentionally closing the forensic window. The table is
  append-only by design; evidence is worth more than schema tidiness.

- **Do not commit the `.env` or any rotated credentials** to git.
  `.gitignore` covers `.env` by default; verify before pushing
  remote after any rotation.
