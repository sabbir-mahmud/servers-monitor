# Configuration reference

The dashboard reads its configuration from environment variables, normally via `main/.env`
([`main/.env.example`](../main/.env.example) is a commented starting point). Values below are
the defaults when a variable is unset.

## Required

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | **none** | Signs sessions, derives every agent's command-signing key and encrypts stored secrets. Long and random; keep it stable (see [rotation](security.md#rotating-secret_key)). |
| `REDIS_URL` | **none** in production | Admin connection to the relay, e.g. `rediss://default:…@….upstash.io:6379`. Must be allowed to run `ACL SETUSER`. Never leaves the dashboard machine. |

## Core

| Variable | Default | Description |
|---|---|---|
| `DEBUG` | `False` | Development mode. Never in production. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Hostnames/IPs the UI may be opened with, comma-separated. |
| `CSRF_TRUSTED_ORIGINS` | *(empty)* | Full origins (`https://monitor.example.com`) when served through a proxy or Tailscale. |
| `SITE_NAME` | `Server Monitor` | Shown in the UI, emails and notifications. |
| `TIME_ZONE` | `UTC` | Zone for maintenance windows and displayed times, e.g. `Asia/Dhaka`. |
| `DASHBOARD_URL` | *(empty)* | How people open the dashboard. Used for links in emails, invitations, reset links and alerts. Recommended in production. |
| `ADMIN_URL` | `admin/` | Path of Django admin (superusers only). |
| `LOG_LEVEL` | `INFO` | Root log level. |

## Transport security

| Variable | Default | Description |
|---|---|---|
| `HTTPS` | `True` when `DEBUG=False` | Secure cookies, HSTS and redirect to HTTPS. Set `False` only for plain HTTP on this machine or a trusted LAN. |
| `TRUST_PROXY_HEADERS` | `False` | Trust `X-Forwarded-For`/`X-Forwarded-Proto`. Only behind a proxy that overwrites them. |
| `SECURE_SSL_REDIRECT` | `True` | With `HTTPS=True`: redirect plain HTTP to HTTPS. |
| `SECURE_HSTS_SECONDS` | `31536000` | With `HTTPS=True`: HSTS max-age. |
| `SECURE_HSTS_PRELOAD` | `False` | Opt in to the HSTS preload list (affects every subdomain, for months). |
| `SESSION_COOKIE_AGE` | `43200` | Session lifetime in seconds (12 hours). |

## Database

| Variable | Default | Description |
|---|---|---|
| `SQLITE_PATH` | `main/db.sqlite3` | Database file. `/data/db.sqlite3` in Docker. |
| `DB_CONN_MAX_AGE` | `600` | Seconds to reuse a database connection. |
| `BACKUP_DIR` | *(next to the database)* `backups/` | Where the relay process writes its daily snapshot. |
| `BACKUP_KEEP` | `14` | Snapshots to keep. |
| `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_REGION`, `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY` | *(empty)* | Off-site replication with the optional `litestream` service (`docker compose --profile backup`). |

## Dashboard health

| Variable | Default | Description |
|---|---|---|
| `WATCHDOG_URL` | *(empty)* | Pinged every minute by the relay process (e.g. a [Healthchecks.io](https://healthchecks.io) check). If the pings stop, that service tells you monitoring is down. **Strongly recommended.** |
| `RELAY_FREE_COMMANDS` | `500000` | Redis commands per month included free, for the usage meter on the System page. |
| `RELAY_PRICE_PER_100K` | `0.2` | Price per 100,000 commands above that, in dollars. |

## Relay and agents

| Variable | Default | Description |
|---|---|---|
| `RELAY_KEY_PREFIX` | `sm` | Prefix of every relay key (`sm:a:<server>:cmd`). Change to share one Redis between dashboards. |
| `RELAY_PUBLIC_HOST` | *(empty)* | Host agents should use if it differs from `REDIS_URL`'s. |
| `RELAY_BLOCK_SECONDS` | `60` | How long the relay process waits per Redis call. Longer = fewer billed commands when idle. |
| `AGENT_REPO` | `sabbir-mahmud/servers-monitor` | GitHub repository agents install from. Point it at your fork. |
| `AGENT_REF` | `main` | Branch, tag or commit agents install from. **Pin a release tag in production.** |
| `AGENT_DIST_DIR` | `<repo>/agent` | Local copy of the agent files. Their checksums go into install commands. |

## Monitoring behaviour

| Variable | Default | Description |
|---|---|---|
| `MONITOR_LATE_AFTER_INTERVALS` | `2` | A server is **Late** after this many missed heartbeats (+ grace). |
| `MONITOR_OFFLINE_AFTER_INTERVALS` | `6` | …and **Offline** after this many. |
| `MONITOR_GRACE_SECONDS` | `10` | Network jitter allowance added to both. |
| `MONITOR_METRIC_RETENTION_DAYS` | `14` | Raw metric samples and application check history kept this long. |
| `ROLLUP_RETENTION_DAYS` | `400` | Hourly server rollups and daily application uptime kept this long: charts up to 90 days, per-mount disk forecasts and status-page history. About 24 rows per server per day. |
| `MONITOR_COMMAND_DELIVERY_SECONDS` | `300` | Queued jobs not picked up within this window expire instead of running late. |
| `MONITOR_COMMAND_OUTPUT_LIMIT` | `262144` | Bytes of output kept per command (the tail is kept). |
| `MONITOR_CHECK_WORKERS` | `8` | Application health checks run in parallel. |
| `MONITOR_ALLOW_PRIVATE_TARGETS` | `False` | Let health checks and webhooks reach private/loopback addresses (to monitor LAN services). Cloud metadata stays blocked. |

## Alert thresholds

| Variable | Default | Description |
|---|---|---|
| `ALERT_CPU_PERCENT` | `90` | Open a CPU alert at this 3-sample average. |
| `ALERT_MEMORY_PERCENT` | `90` | Memory alert threshold. |
| `ALERT_DISK_PERCENT` | `90` | Fullest-disk alert threshold. |

Notification channels are configured in the UI, not here.

| Variable | Default | Description |
|---|---|---|
| `WEBPUSH_CONTACT` | *(from `DEFAULT_FROM_EMAIL`)* | The `mailto:` or `https:` contact browser push services see for this dashboard (VAPID). Browser push needs the dashboard on HTTPS (or `localhost`). |

## REST API

| Variable | Default | Description |
|---|---|---|
| `API_RATE_LIMIT` | `120` | Requests per minute per API token, for `/api/v1/` and `/metrics`. See [api.md](api.md). |

## Accounts

| Variable | Default | Description |
|---|---|---|
| `ALLOW_SIGNUP` | `False` | Public sign-up. New accounts wait for Admin approval with no access. |
| `REQUIRE_2FA_FOR_ADMINS` | `True` | Admins must set up an authenticator app before using the dashboard. Others can opt in under *My account*. |
| `INVITATION_DAYS` | `7` | Lifetime of invitation links. |
| `PASSWORD_RESET_TIMEOUT` | `7200` | Lifetime of password-reset links, in seconds. |
| `LOGIN_FAILURE_LIMIT` | `6` | Failed sign-ins per account + IP before a lock… |
| `LOGIN_ACCOUNT_FAILURE_LIMIT` | `20` | …per account from any IP… |
| `LOGIN_IP_FAILURE_LIMIT` | `50` | …and per IP across accounts. |
| `LOGIN_FAILURE_TIMEOUT` | `900` | Window and lock duration, in seconds. |
| `SECRET_KEY_FALLBACKS` | *(empty)* | Previous `SECRET_KEY` values, comma-separated, during a rotation. |

## Single sign-on

Sign in with any OpenID Connect provider (Keycloak, Authentik, Okta, Google Workspace,
Microsoft Entra ID…). On when `OIDC_RP_CLIENT_ID` is set; the sign-in page then shows
**Sign in with …** next to the password form. Register the dashboard at your provider as a
confidential web client with the redirect URI `https://<dashboard>/oidc/callback/`; the
endpoint URLs are in the provider's `/.well-known/openid-configuration`.

| Variable | Default | Description |
|---|---|---|
| `OIDC_RP_CLIENT_ID` | *(empty)* | Client ID. Setting it turns SSO on. |
| `OIDC_RP_CLIENT_SECRET` | *(empty)* | Client secret. |
| `OIDC_OP_AUTHORIZATION_ENDPOINT` | *(empty)* | `authorization_endpoint` from discovery. |
| `OIDC_OP_TOKEN_ENDPOINT` | *(empty)* | `token_endpoint`. |
| `OIDC_OP_USER_ENDPOINT` | *(empty)* | `userinfo_endpoint`. |
| `OIDC_OP_JWKS_ENDPOINT` | *(empty)* | `jwks_uri`: ID tokens are checked against these keys. |
| `OIDC_RP_SIGN_ALGO` | `RS256` | ID token signature algorithm (`RS256`, `ES256`…). |
| `OIDC_RP_SCOPES` | `openid email profile` | Add your provider's groups scope if it needs one (e.g. `groups`). |
| `OIDC_PROVIDER_NAME` | `single sign-on` | Shown on the button: "Sign in with Okta". |
| `OIDC_GROUPS_CLAIM` | `groups` | The claim listing the person's groups. |
| `OIDC_ADMIN_GROUPS`, `OIDC_OPERATOR_GROUPS`, `OIDC_VIEWER_GROUPS` | *(empty)* | Comma-separated groups for each role. The highest matching role wins, and it's re-applied at every sign-in. |
| `OIDC_DEFAULT_ROLE` | *(empty)* | Role for people in no mapped group (`Viewer`, …). Empty: they can't sign in. |
| `OIDC_CREATE_USERS` | `True` | Create accounts on first sign-in (no invitation needed). `False`: only people who already have an account (matched by verified email) can use SSO. |
| `OIDC_ALLOW_UNVERIFIED_EMAIL` | `False` | Accept emails the provider doesn't mark `email_verified` (needed for some Entra ID setups). Only if you trust every account at the provider. |

How accounts are matched and protected:

- A first SSO sign-in links to an existing account with the same **verified** email; after
  that the provider's subject (`sub`) is what counts, so an email change there is harmless
  and nobody else can claim the account with the same email.
- Linked accounts can't use a local password any more: removing someone at the provider
  locks them out at their next sign-in (existing sessions end within `SESSION_COOKIE_AGE`).
- **Superusers never sign in through SSO.** Keep one as a local break-glass account; local
  Admins still need two-factor. SSO sessions rely on the provider's own MFA.

## Email

Leave `EMAIL_HOST` empty to run without email: Admins then hand out reset and invitation
links from the Users page.

| Variable | Default | Description |
|---|---|---|
| `EMAIL_HOST` | *(empty)* | SMTP server. Setting it enables email. |
| `EMAIL_PORT` | `587` | SMTP port. |
| `EMAIL_HOST_USER` | *(empty)* | SMTP username. |
| `EMAIL_HOST_PASSWORD` | *(empty)* | SMTP password or app password. |
| `EMAIL_USE_TLS` | `True` | STARTTLS. |
| `EMAIL_USE_SSL` | `False` | Implicit TLS (port 465). Use one or the other. |
| `DEFAULT_FROM_EMAIL` | `Server Monitor <noreply@localhost>` | Sender address. |

## Agent configuration

Agents are configured on each server in `/etc/server-monitor-agent/agent.toml`, normally
through `smctl`. See [agent.md](agent.md) and
[`agent/agent.example.toml`](../agent/agent.example.toml). The options added in 2.2:

| Setting | Default | `smctl` | Description |
|---|---|---|---|
| `[agent] inventory` | `true` | `smctl edit` | Top processes, temperatures, GPUs and containers in each heartbeat (read-only). |
| `[docker] names` | `false` | `smctl docker enable` | Container names and stopped containers via `docker ps` (the docker group is root-equivalent). |
| `[probes] enabled` | `false` | `smctl probes on` | Run HTTP/TCP uptime checks the dashboard assigns, from this server. |
