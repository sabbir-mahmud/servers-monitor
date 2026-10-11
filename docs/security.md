# Security model

Server Monitor can run commands on every server it watches, so it's built on the assumption
that any one part (the dashboard, the Redis relay, a monitored server, the GitHub repository)
might be compromised, and limits what that part can do.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What each party can do

| If an attacker controls… | They can | They can't |
|---|---|---|
| **The Redis relay** (admin password or provider account) | Read relay traffic, delay or drop messages | Run any command or forge any result: messages are signed with keys Redis never sees |
| **One monitored server** | Send its own heartbeats and results | Read or affect any other server (its Redis user is confined to its two keys), or flood the dashboard (per-server rate limits, 256 KB message cap) |
| **A dashboard account** | What its role allows | Anything on a server beyond that server's allowlist |
| **An API token** | What its owner's role allows, limited to its scope (read, or read + run actions) | Free-form shell (browser-only); anything after it's revoked, expired or its owner is deactivated |
| **The dashboard machine itself** | Ask every server to run allowlisted actions (and shell, where enabled) | Run anything a server's `agent.toml` doesn't declare |
| **The GitHub repository** | Publish modified agent code | Get it installed: install and upgrade commands verify checksums from your deployed dashboard |

## Signed commands

Each server has a signing key `HMAC-SHA256(SECRET_KEY, "agent-signing:v1:<server id>:<salt>")`.
The salt is stored; the key is not. The key reaches the agent once, inside the connection
string shown when the server is added or its credentials are rotated.

- The dashboard signs every message to the agent, and the agent refuses anything unsigned or
  mis-signed.
- The agent signs everything it sends, and the dashboard drops forged heartbeats and results.
- The agent keeps a replay list of command IDs it has run in `/var/lib/server-monitor-agent`
  (systemd `StateDirectory`), so a captured command can't be replayed, even after a restart.
  Commands also expire after 5 minutes.

Servers enrolled before signing existed are shown with a warning on their page. Rotate their
credentials and run `sudo smctl connect` to upgrade them.

## Least privilege on servers

- The agent runs as the unprivileged `server-monitor` user.
- It runs only actions declared in `agent.toml`, as argv lists with no shell. Arguments from
  the dashboard never change them. Logs can only be tailed if they're declared.
- Free-form shell is off by default, needs `[shell] enabled = true` on the server **and** the
  Admin role in the dashboard, and is audited.
- **Uptime probes** are off by default (`sudo smctl probes on`). With them on, the dashboard
  can have the agent make HTTP GET/HEAD/POST/PUT requests and TCP connections to hosts it
  names, including internal ones: that's their purpose. The agent itself refuses cloud
  metadata, link-local, unspecified and multicast addresses, checks the address it connects
  to (after DNS, and on every redirect), and never receives secret request headers (relay
  messages are signed, not encrypted; the form refuses headers on agent-checked monitors).
  Probe results only count for monitors that list that server.
- **Live log follow** only follows logs declared in `[logs]` and journals of declared
  `systemd` services, for at most 10 minutes, with output bounded like any command.
- Root-level actions get one exact-command `NOPASSWD` sudo rule each, validated with `visudo`.
  Unit names and arguments are restricted so they can't widen a rule.
- The agent refuses unencrypted Redis connections unless explicitly allowed for local
  development.

## Verified installs

The dashboard computes SHA-256 checksums of the agent files it was deployed with
(`AGENT_DIST_DIR`). Install and upgrade commands:

1. download `install.sh` and check its checksum before running it,
2. pass the other files' checksums to the installer, which refuses any download that doesn't
   match,
3. read the connection string from a hidden prompt (or a root-only file), never from the
   command line.

Pin `AGENT_REF` to a release tag in production.

**Upgrades from the dashboard** are opt-in per server (`sudo smctl remote-upgrade on`). The
agent can only drop a request file; a root systemd service re-validates it (release tag of the
recorded repository, SHA-256 checksums, untrusted-file handling) before running the same
verified installer. A compromised agent user could at most request another release tag.
See [agent.md](agent.md#upgrading-and-re-keying).

## Accounts and the web UI

- **Roles:** Viewer, Operator and Admin. Superusers can only be changed by superusers.
- **Invite-only** by default. Invitation tokens and pending credentials are stored hashed or
  encrypted, are single-use and expire.
- **Passwords:** at least 10 characters plus Django's similarity, common-password and numeric
  checks. Changing your password signs out your other sessions. Changing your email needs the
  current password and notifies the old address.
- **"Forgot password"** gives the same response, in the same time, whether or not the address
  has an account.
- **Throttling:** sign-in is limited per account + IP, per account and per IP (Django admin's
  login uses the same throttle). Resets, sign-ups and invitation links are rate-limited.
- **Two-factor sign-in** (authenticator app, single-use recovery codes) is required for
  Admins by default. Break-glass on the host: `manage.py reset_2fa <user>`.
- **Single sign-on (OIDC)** uses the authorization-code flow with state, nonce and PKCE, and
  checks the ID token's signature against the provider's keys. Accounts are matched by the
  provider's subject, linked once by *verified* email; superusers never sign in through it;
  linked accounts can't fall back to a local password, so removing someone at the provider
  locks them out. See [configuration.md](configuration.md#single-sign-on).
- **API tokens** are stored as SHA-256 hashes, shown once, can expire and be revoked, and are
  rate-limited per token (and bad tokens per IP). The API never accepts the session cookie,
  so it can't be driven by cross-site requests. See [api.md](api.md).
- **Web hardening:** CSRF on every form, a Content Security Policy with nonce-only scripts,
  `X-Frame-Options: DENY`, no open redirects, and HTTPS with HSTS and secure cookies by default
  in production.
- **Audit log:** every sign-in, account change, server change, credential rotation, job,
  cancellation, monitor change, channel change, maintenance window, schedule, rollout
  resume/abort, API token and SSO role change. Jobs started through the API record the
  token. Append-only.

## Outbound requests (SSRF)

Health checks and webhooks are URLs typed in by people. They go through a network guard that
checks the address actually connected to (after DNS, and again on every redirect), allows only
`http` and `https`, and refuses private, loopback and link-local addresses.
`MONITOR_ALLOW_PRIVATE_TARGETS=True` permits private networks for LAN monitoring; cloud
metadata (`169.254.0.0/16`) stays blocked regardless.

## Data at rest

- Notification channel secrets, monitor request headers, TOTP secrets, the Web Push (VAPID)
  private key and pending enrolment credentials are encrypted with Fernet, using a key derived
  from `SECRET_KEY` via HKDF.
- Agent Redis passwords, invitation tokens, recovery codes and API tokens are stored only as
  SHA-256 hashes.
- Browser push messages are end-to-end encrypted to the browser (RFC 8291): the push service
  sees only ciphertext.
- Agent signing keys aren't stored at all; they're derived when needed.

Protect `main/.env` and the SQLite file like the credentials they are.

## Rotating SECRET_KEY

1. Set the new `SECRET_KEY` and put the old one in `SECRET_KEY_FALLBACKS`. Sessions,
   encrypted secrets and incoming agent signatures keep working.
2. Rotate each server's credentials and run `sudo smctl connect` on it. Commands are signed
   with the new key, so un-rotated agents stop accepting them.
3. Re-save each notification channel (open it, press Save) to re-encrypt it, then remove the
   old key from `SECRET_KEY_FALLBACKS`.

## Hardening checklist

- [ ] `DEBUG=False`, a long random `SECRET_KEY`, `ALLOWED_HOSTS` set
- [ ] HTTPS in front (Tailscale, Cloudflare Tunnel or Caddy) and `HTTPS=True`
- [ ] `AGENT_REF` pinned to a release tag
- [ ] Every server shows signed commands (no legacy warning on its page)
- [ ] `[shell] enabled = false` unless you really need it
- [ ] Every Admin has two-factor on (or signs in through SSO with the provider's MFA)
- [ ] A local superuser kept for break-glass, with a strong password and 2FA
- [ ] API tokens: read-only where possible, with an expiry
- [ ] `WATCHDOG_URL` set, and backups (plus Litestream) on
- [ ] Telegram or webhook notifications configured and tested
- [ ] Regular backups of the SQLite file **and** `main/.env`
- [ ] `make check` passes (Django's production deployment audit)
