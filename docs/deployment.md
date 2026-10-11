# Deploying Server Monitor

The dashboard is two processes sharing one SQLite database:

| Process | Command | Job |
|---|---|---|
| **web** | `gunicorn config.wsgi` (1 worker, 8 threads) | The UI |
| **relay** | `python manage.py relay` | Receives agent heartbeats and results, sends jobs, runs health checks, sends notifications, detects offline servers, prunes old data. **Run exactly one.** |

Neither needs to be reachable from the internet. Both dial out to your Redis relay.

- [1. Choose a Redis relay](#1-choose-a-redis-relay)
- [2. Configure](#2-configure)
- [3a. Docker (recommended)](#3a-docker-recommended)
- [3b. systemd, without Docker](#3b-systemd-without-docker)
- [4. HTTPS and remote access](#4-https-and-remote-access)
- [5. First steps](#5-first-steps)
- [Email](#email)
- [Backups](#backups)
- [Upgrading](#upgrading)
- [Sizing and cost](#sizing-and-cost)

## 1. Choose a Redis relay

Any Redis 6+ that supports **TLS** and **`ACL SETUSER`** works. Each server gets its own
Redis user restricted to its own keys, so the admin credentials in `REDIS_URL` must be allowed
to create users.

| Option | Notes |
|---|---|
| **Upstash** (easiest) | Create a database, copy the `rediss://default:…@….upstash.io:6379` URL. The free tier (500K commands/month) covers about 6 servers. |
| **Self-hosted Redis / Valkey** | Enable TLS if servers connect over the internet. On a trusted LAN, plain `redis://` works with `allow_insecure` on the agent. |
| Redis Cloud, Aiven | Usually fine if your plan allows `ACL SETUSER`. |
| AWS ElastiCache, Azure Cache | Usually **not** supported: they manage users outside Redis and block `ACL SETUSER`. |

If the provider refuses, **Add server** shows the exact error.

## 2. Configure

```bash
cp main/.env.example main/.env
```

The minimum:

```ini
SECRET_KEY=<long random string>          # python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
REDIS_URL=rediss://default:<password>@<host>:6379
ALLOWED_HOSTS=localhost,127.0.0.1        # add every hostname/IP you'll open the UI with
TIME_ZONE=Asia/Dhaka                     # maintenance windows are entered in this zone
DASHBOARD_URL=https://monitor.example.com  # how people open it; used in links in emails/alerts
```

`HTTPS` defaults to **on** when `DEBUG=False`: secure cookies, HSTS and a redirect to HTTPS.
If you serve plain HTTP (on this machine, or a trusted LAN), set `HTTPS=False` explicitly.
See [section 4](#4-https-and-remote-access).

Every variable is listed in [configuration.md](configuration.md).

> **Keep `SECRET_KEY` safe and stable.** It signs sessions, derives every agent's
> command-signing key and encrypts stored secrets. To rotate it, put the old value in
> `SECRET_KEY_FALLBACKS`, then rotate each server's credentials (see
> [security.md](security.md#rotating-secret_key)).

## 3a. Docker (recommended)

Works on any x86_64 or ARM64 machine, including a Raspberry Pi 4/5 with a 64-bit OS.

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

- The UI listens on **127.0.0.1:8000** only. To open it to your LAN, change the port mapping in
  `docker-compose.yml` to `"8000:8000"` and add the machine's LAN IP to `ALLOWED_HOSTS`.
- Data lives in the `data` volume (`/data/db.sqlite3`).
- `web` applies migrations on start; `relay` starts once `web` is healthy.
- Memory limits: web 256 MB, relay 128 MB.

## 3b. systemd, without Docker

```bash
sudo useradd --system --create-home --home-dir /opt/server-monitor server-monitor-ui
sudo -u server-monitor-ui git clone https://github.com/sabbir-mahmud/servers-monitor.git /opt/server-monitor
cd /opt/server-monitor
sudo -u server-monitor-ui make install
sudo -u server-monitor-ui mkdir -p data
sudo -u server-monitor-ui cp main/.env.example main/.env   # then edit it
```

In `main/.env` also set `SQLITE_PATH=/opt/server-monitor/data/db.sqlite3`. Then:

```bash
sudo cp deploy/server-monitor-web.service deploy/server-monitor-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor-web server-monitor-relay
sudo -u server-monitor-ui venv/bin/python main/manage.py createsuperuser
```

The web unit binds to `127.0.0.1:8000`; put a reverse proxy in front (next section).

## 4. HTTPS and remote access

Pick one. None of them needs an open inbound port on your router.

**Tailscale (simplest private access).** Install Tailscale on the dashboard machine and on the
devices you'll open it from, then:

```bash
sudo tailscale serve --bg 8000
```

Set `HTTPS=True`, `ALLOWED_HOSTS=<machine>.<tailnet>.ts.net` and
`CSRF_TRUSTED_ORIGINS=https://<machine>.<tailnet>.ts.net`.

**Cloudflare Tunnel + Cloudflare Access.** Gives you a public hostname behind Cloudflare's
login. Put an Access policy in front so only your team can reach the login page.

**Caddy on a VPS.** Automatic Let's Encrypt certificates:

```caddyfile
monitor.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Set `HTTPS=True` and `TRUST_PROXY_HEADERS=True`, because Caddy overwrites `X-Forwarded-*`.

**Plain HTTP on a trusted network.** Set `HTTPS=False`. Session cookies then cross the network
unencrypted, and Admins see a warning whenever the dashboard is opened that way from another
machine.

Over HTTPS the dashboard is also an **installable app** (Install app in the sidebar, or Add to
Home Screen on a phone) and can send **browser push** alerts (Notifications → Add → Browser
push, once per device). Browsers only allow both on HTTPS or `localhost`.

## 5. First steps

1. Sign in, open **My account** (bottom of the sidebar) and add your email.
2. **Servers → Add server**, then run the verified command it shows on that server.
3. **Incidents → Notifications → Add Telegram** so alerts reach your phone.
4. **Applications → Add application** for each site or API health URL.
5. **Administration → Users → Invite someone** for your team.

## Email

Email is optional. Without it, Admins create password-reset and invitation links in the
**Users** page and send them themselves. With SMTP configured, people reset their own
passwords and invitations are emailed:

```ini
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=…
EMAIL_HOST_PASSWORD=…
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=Server Monitor <monitor@example.com>
```

Any SMTP provider works (Gmail with an app password, Brevo, Mailgun, Postmark, Amazon SES).

## Backups

All state is one SQLite file. Two layers protect it:

**Daily snapshots (automatic).** The relay process takes a consistent snapshot every 24 hours
into `BACKUP_DIR` (default: a `backups/` folder next to the database) and keeps the last
`BACKUP_KEEP` (14). *Administration → System* shows the latest one and warns when it's over
36 hours old. Take one now with `python manage.py backup`.

Snapshots sit on the same disk as the database, so copy them elsewhere, or use:

**Continuous off-site replication (optional).** [Litestream](https://litestream.io) streams
every change to any S3-compatible bucket (Cloudflare R2, Backblaze B2, AWS S3) within seconds.
Set the `LITESTREAM_*` variables in `main/.env` (see [configuration](configuration.md)), then:

```bash
docker compose --profile backup up -d
```

**Restoring** a snapshot:

```bash
docker compose stop web relay
docker compose run --rm web python manage.py restore /data/backups/db-20261011-031500.sqlite3
docker compose start web relay
```

`restore` refuses while the relay is running and checks the file's integrity first; the
database it replaces is kept beside it. From Litestream, restore with
`litestream restore -config deploy/litestream.yml -o /data/db.sqlite3 /data/db.sqlite3`
with both processes stopped.

Keep `main/.env` (above all `SECRET_KEY`) with your backups: encrypted secrets and agent
signing keys can't be recovered without it. The relay holds only in-flight messages; nothing
there needs backing up.

## Watching the dashboard itself

The dashboard can't tell you it has stopped. Create a free check at
[healthchecks.io](https://healthchecks.io) (period 1 minute, grace 5 minutes) and put its ping
URL in `WATCHDOG_URL`. The relay process pings it every minute; if the machine goes down, that
service alerts you. When the relay starts again after more than 5 minutes away, every
notification channel is told how long monitoring was blind.

## Upgrading

```bash
git pull
docker compose up -d --build      # or: make install && sudo systemctl restart server-monitor-web server-monitor-relay
```

Migrations run automatically. If the release changed anything under `agent/` (the
[changelog](../CHANGELOG.md) says **agents must be upgraded**), upgrade each server with the
command on its page (**⋯ → Install / upgrade agent**). Older agents keep working; their
server page lists what upgrading unlocks.

> Install and upgrade commands only accept agent files whose checksums match the dashboard
> you deployed. Deploy the dashboard from the same commit you'll install agents from, or pin
> `AGENT_REF` to a release tag.

## Sizing and cost

| | |
|---|---|
| Dashboard | ≈ 200 MB RAM in total; SQLite comfortably handles hundreds of servers |
| Redis commands | ≈ 65K per server per month at the default 2-minute heartbeat, plus ≈ 45K/month for the idle relay |
| Upstash cost | Free up to about 6 servers; pay-as-you-go is $0.20 per 100K commands (10 servers ≈ $1.40/month) |
| Metrics retention | 14 days of raw samples (`MONITOR_METRIC_RETENTION_DAYS`), 400 days of hourly rollups (`ROLLUP_RETENTION_DAYS`, ≈ 10K rows per server) |
| Uptime probes from agents, live log follow | Probes add no Redis commands (results ride on heartbeats); following a log costs ≈ 1 command every 3 s while it runs |

Raise a server's heartbeat interval on its edit page to spend fewer commands. Jobs still
arrive instantly, because agents block on their mailbox between heartbeats.
