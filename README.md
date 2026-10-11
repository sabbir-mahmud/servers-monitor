# Server Monitor

One Django dashboard for many servers: live health, services and versions, logs, and
running the same deploy or update on many servers at once. **No public API, no SSH, no
open ports**: the dashboard can run on your laptop or a Raspberry Pi at home, and the
servers can be anywhere.

| Part | Where it runs | What it is |
|---|---|---|
| **main/** | any machine you own (laptop, home server, Pi, VPS) | Django 6.1 + HTMX, SQLite, two processes: `web` and `relay` |
| **relay** | Upstash Redis (or any TLS Redis) | a mailbox both sides dial out to |
| **agent/** | every monitored server | one Python file + `psutil`, systemd, managed with `smctl` |

```
   your machine (no inbound ports)        Upstash Redis (TLS)          your servers (no inbound ports)
 ┌────────────────────────────────┐     ┌────────────────────┐       ┌──────────────────────────┐
 │ web    : Django UI 127.0.0.1   │     │ sm:a:<id>:cmd  ◀───┼─push──│                          │
 │ relay  : BLPOP all inboxes ────┼────▶│ sm:a:<id>:in   ────┼─BLPOP─│ agent (ACL user sm-<id>: │
 │ SQLite : servers, jobs, audit  │     │   ...one pair per  │       │  only its own two keys)  │
 └────────────────────────────────┘     │   server           │       └──────────────────────────┘
          dials out ──────────────────▶ └────────────────────┘ ◀────────────────── dials out
```

## How it works

1. **Nobody connects in.** The dashboard and every agent open outbound TLS connections
   to Redis. Jobs go into `sm:a:<id>:cmd`; the agent is blocked on `BLPOP` there, so a
   queued deploy starts in under a second. Heartbeats, acks and streamed output go into
   `sm:a:<id>:in`, which the dashboard's `relay` process `BLPOP`s for the whole fleet.
2. **Each server is fenced off.** "Add server" creates a Redis ACL user `sm-<id>` that may
   only `RPUSH`/`BLPOP`/`LTRIM`/`PING` on its own two keys: no other server's keys, no
   `GET`/`KEYS`/`DEL`/`FLUSH`/`ACL`. The admin `REDIS_URL` never leaves your machine.
   Deleting a server deletes its Redis user; disabling it turns the user off.
3. **The agent's config is the security boundary.** The dashboard sends an action *name*;
   the agent runs it only if its local allowlist declares it, as a fixed argv with no
   shell. Free-form shell is off by default and also needs the Admin role. Even someone
   with your Redis admin password can only trigger what each server already allows.
4. **At-most-once jobs.** A job is popped by exactly one `BLPOP`. If a running job goes
   silent past its timeout it becomes **Lost** and is never re-sent. Jobs queued for an
   offline server wait in its mailbox for 5 minutes, then **expire** (the agent also
   refuses stale ones). Cancelling a queued job removes it from the mailbox (`LREM`),
   which proves the agent never saw it. If Redis is unreachable when you click, the job
   waits in a database outbox and the relay process retries it.
5. **Health needs nothing to fire.** It's derived from `last_seen_at`: Online ≤ 2 intervals
   + 10 s, Late ≤ 6 intervals + 10 s, otherwise Offline. Interval changes in the dashboard
   are pushed to the agent immediately.
6. **Alerts** are deduplicated in the database (one open alert per server/kind/subject),
   CPU uses a 3-sample average, thresholds have 5 % hysteresis, offline fires on the
   state change. Webhook notifications (Slack/Discord/Teams) go out from a background
   thread with retries.
7. **Audit everything.** Logins, server changes, credential rotations, every job (targets
   and skipped servers) and cancellations, append-only.

### Redis usage and cost

An idle agent costs about two Redis commands per heartbeat (one `RPUSH`, one `BLPOP` that
waits up to 120 s), and the dashboard pops each heartbeat once. At the default 2-minute
interval that's **~65K commands per server per month**, plus ~45K/month for the idle relay
process. Upstash's free tier (500K/month) covers about 6 servers; beyond that,
pay-as-you-go is $0.20 per 100K commands (10 servers ≈ $1.40/month, 50 ≈ $6.60/month, even billed from zero). Jobs and
streamed output add a few commands each. Raise the interval on quiet servers to save more.

## Features

- **Dashboard:** every server with health, CPU, memory, fullest disk, load, network,
  uptime and last report. Filter by environment, group, health or name. Refreshes every 10 s.
- **Server page:** live tiles, services with state and version (systemd, docker, pm2,
  process, http), disks, open alerts, and 1h/6h/24h/7d history charts with hover. Gaps
  in the charts mean the server was down.
- **Actions:** one-click custom actions (deploy, update…), restart buttons for services
  marked restartable, and a log tail viewer. Optional audited shell for admins.
- **Run on servers:** pick an action, tick servers (or select all), queue it, then watch
  per-server status and output live. Servers that can't run the action are skipped and
  the reason is shown.
- **Applications:** HTTP(S) health checks for your apps (`/apps/`). Each one has a URL,
  an interval (30 s to 1 h), the status codes that count as up, an optional keyword the
  body must contain, and an optional link to the server it runs on. You get a 24-hour
  uptime bar, uptime over 24 h, 7 d and 14 d, a response-time chart, incidents, and TLS
  certificate expiry (warning under 14 days). A failed check is retried after 15 s and it
  only counts as down after N failures in a row, so a blip never pages you. Checks run
  from the relay process over plain HTTPS, so they cost no Redis commands. For URLs only
  reachable from inside a server (`localhost:8000/health`), use
  `smctl service add --kind http` on that server instead.
- **Alerts:** offline, CPU, memory, disk, service down, application down, certificate expiring. Acknowledge, resolve
  automatically, notify via webhook.
- **Roles:** Viewer (read), Operator (run actions, ack alerts), Admin (manage servers
  and tokens, shell, audit log). Defined in `apps/users/roles.py` and synced on every migrate.

## Deploying the dashboard

Everything below runs on **your** machine; nothing needs a public address.

**1. Configure**: `cp main/.env.example main/.env`, then set `SECRET_KEY`
(`python3 -c 'import secrets; print(secrets.token_urlsafe(50))'`) and `REDIS_URL` (Upstash:
the `rediss://default:…@….upstash.io:6379` URL). Keep `HTTPS=False` unless you put TLS in
front.

**2a. Docker** (laptop, home server, Pi):
```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
open http://127.0.0.1:8000
```
Two containers from one image: `web` (Gunicorn, 1 worker × 8 threads, ≤256 MB) and `relay`
(≤128 MB), sharing a SQLite volume. The UI binds to 127.0.0.1; change the port mapping to
`8000:8000` and add your LAN IP to `ALLOWED_HOSTS` to reach it from other devices.

**2b. Without Docker** (systemd): clone to `/opt/server-monitor`, `make install`, set
`SQLITE_PATH=/opt/server-monitor/data/db.sqlite3` in `.env`, then install
[`deploy/server-monitor-web.service`](deploy/server-monitor-web.service) and
[`deploy/server-monitor-relay.service`](deploy/server-monitor-relay.service).

**Reaching the UI from elsewhere** without opening ports: [Tailscale](https://tailscale.com)
(`tailscale serve 8000`, then `HTTPS=True`) or a Cloudflare Tunnel with Cloudflare Access in
front. Run exactly **one** `relay` process.

**Backups**: the whole state is one SQLite file (`sqlite3 db.sqlite3 ".backup b.sqlite3"`).
The relay holds only in-flight messages.

## Adding a server

The agent installs from **this GitHub repo** (it must be public, or set `AGENT_REPO` to a
public fork). "Add server" prints one command with that server's own Redis credentials:

```bash
curl -fsSL https://raw.githubusercontent.com/sabbir-mahmud/servers-monitor/main/agent/install.sh \
  | sudo SM_REF=main SM_REDIS='rediss://sm-<id>:<password>@<name>.upstash.io:6379' bash
```

It installs python3-venv if missing, downloads the agent and `smctl` from GitHub, logs in
to the relay **before** touching any config, installs the systemd service, detects running
services (nginx, Postgres, Redis, gunicorn, your own units…) and log files, and confirms a
real heartbeat. The page flips to **Connected** by itself. Pin `AGENT_REF` (and so
`SM_REF`) to a release tag for reproducible installs. Re-running upgrades in place.

### `smctl`: managing a server afterwards

```bash
sudo smctl status | doctor | list | detect --apply
sudo smctl action add deploy -d "Pull and restart" -- /opt/deploy/deploy.sh
sudo smctl action add upgrade --sudo -- /usr/bin/apt-get -y upgrade   # root, exact sudo rule
sudo smctl action test deploy                          # run it here as the agent user
sudo smctl service restartable nginx on                # Restart button + sudo rule
sudo smctl log add api /var/www/api/logs/app.log
sudo smctl connect 'rediss://sm-…'                     # after rotating credentials
sudo smctl shell on|off                                # free-form shell (Admin role, audited)
sudo smctl edit | logs -f | upgrade [--ref v1.2] | uninstall [--purge]
```

Every change is validated by the agent itself before it's written, sudo rules are one
exact command per line (checked with `visudo`), and the agent restarts in well under a
second. Docker containers are only monitored after `sudo smctl docker enable`, because the
docker group is root-equivalent. [`agent/agent.example.toml`](agent/agent.example.toml)
documents every option.

## Development

```bash
make install
cp main/.env.example main/.env     # DEBUG=True, SECRET_KEY, REDIS_URL
make migrate superuser
make run                           # http://127.0.0.1:8000
make relay                         # second terminal
```

For a local agent, create `agent/dev.toml` with the `redis_url` from "Add server" and run
`make agent-dev`. `python agent/server_monitor_agent.py -c agent/dev.toml --print` prints
the report the agent would send; `--ping` checks the relay login.

## Checks

```bash
make test    # dashboard + agent/smctl tests (relay driven by an in-memory Redis)
make lint    # ruff; agent code is held to Python 3.11 syntax (servers' system Python)
make check   # system checks, migrations up to date, check --deploy
```

## Layout

```
main/
  config/            settings (env-driven), urls
  apps/fleet/        Server, ServerGroup, MetricSample · ingestion · dashboard + server views
  apps/agent_api/    Redis relay: per-server ACL users, the bridge (`manage.py relay`),
                     payload validation
  apps/operations/   Job (fan-out) + Command (per server) lifecycle
  apps/alerts/       rules, offline sweep, webhook notifications (thread + retries)
  apps/monitors/     application health checks: checker (runs in the relay), stats, UI
  apps/audit/        append-only audit trail
  apps/users/        custom User, roles matrix, throttled login
  templates/         Bootstrap 5 + HTMX, Django 6 template partials for live regions
agent/
  server_monitor_agent.py   the whole agent (stdlib + psutil)
  smctl.py                  the on-server management command (stdlib only)
  agent.example.toml        annotated allowlist config
  install.sh                installer / upgrader (fetched from GitHub)
  server-monitor-agent.service
```
