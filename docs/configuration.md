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
| `MONITOR_METRIC_RETENTION_DAYS` | `14` | Metric samples and application check history kept this long. |
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

Notification channels (Telegram, webhooks) are configured in the UI, not here.

## Accounts

| Variable | Default | Description |
|---|---|---|
| `ALLOW_SIGNUP` | `False` | Public sign-up. New accounts wait for Admin approval with no access. |
| `INVITATION_DAYS` | `7` | Lifetime of invitation links. |
| `PASSWORD_RESET_TIMEOUT` | `7200` | Lifetime of password-reset links, in seconds. |
| `LOGIN_FAILURE_LIMIT` | `6` | Failed sign-ins per account + IP before a lock… |
| `LOGIN_ACCOUNT_FAILURE_LIMIT` | `20` | …per account from any IP… |
| `LOGIN_IP_FAILURE_LIMIT` | `50` | …and per IP across accounts. |
| `LOGIN_FAILURE_TIMEOUT` | `900` | Window and lock duration, in seconds. |
| `SECRET_KEY_FALLBACKS` | *(empty)* | Previous `SECRET_KEY` values, comma-separated, during a rotation. |

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
[`agent/agent.example.toml`](../agent/agent.example.toml).
