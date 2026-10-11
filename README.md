# Server Monitor

One Django dashboard for many servers: live health, services and versions, logs, and
running the same deploy or update on many servers at once. It has two parts:

| Part | Where it runs | What it is |
|---|---|---|
| **main/** | one control-plane host | Django 6.1 + templates + HTMX (no React), Postgres, Redis, Celery |
| **agent/** | every monitored server | one Python file + `psutil`, run by systemd |

```
                        ┌──────────────── MAIN (Django) ────────────────┐
  Browser ── HTMX ───▶  │ Dashboard · Server detail · Jobs · Alerts ·    │
  (polls 2–30 s)        │ Audit log · Roles (Viewer/Operator/Admin)      │
                        │                                                │
                        │ /api/agent/v1/  heartbeat · commands · result  │
                        │ Celery beat: offline sweep (15 s), prune (1 h) │
                        └──────────────▲─────────────────────────────────┘
                                       │ HTTPS, agent dials OUT only
                     ┌─────────────────┼─────────────────┐
                ┌────┴────┐       ┌────┴────┐       ┌────┴────┐
                │ agent   │       │ agent   │       │ agent   │
                │ web-01  │       │ web-02  │       │ db-01   │
                └─────────┘       └─────────┘       └─────────┘
              agent.toml = this server's allowlist of services, logs, actions
```

## What changed from the ChatGPT design, and why

The conversation ended on the right shape (main + child agent, Django only). These are
the decisions I made beyond it:

1. **Agents only dial out.** Main never connects to a server. It doesn't SSH in and
   there's no open agent port, so servers behind NAT or a firewall just work, and the
   only thing exposed is one authenticated HTTPS endpoint on main. Commands reach the
   agent through a cheap poll (default 5 s) and also ride along on each heartbeat
   response.
2. **The agent's config is the security boundary, not the dashboard.** Main sends an
   action *name*. The agent runs it only if its local `agent.toml` declares it, as a
   fixed argv list with no shell, and arguments from main can never change that argv.
   Free-form shell is off by default on each server and also needs the Admin role.
   If main is compromised, the attacker can only do what each server already allows.
   Main checks too (it only offers actions the agent advertised), so you get two layers.
3. **At-most-once commands.** A command goes to the agent once (`SELECT … FOR UPDATE
   SKIP LOCKED`). If a delivered command never reports back, it becomes **Lost**; it is
   never re-run. A deploy that runs twice is worse than one you re-click. Commands still
   queued after 5 minutes **expire** instead of firing late.
4. **Health is computed from `last_seen_at`; nothing has to fire for it.**
   Online ≤ 2 intervals + 10 s, Late ≤ 6 intervals + 10 s, otherwise Offline. Each
   server's interval is set in the dashboard and sent back in every heartbeat response,
   so the agent retunes itself without SSH.
5. **Alerts are deduplicated in the database.** A partial `UniqueConstraint` allows one
   open alert per (server, kind, subject). CPU alerts use a 3-sample average so one
   spike doesn't page you. Thresholds have 5 % hysteresis. Offline alerts fire on the
   state change, not on every sweep. Notifications go to a Slack/Discord/Teams webhook
   through Celery with retries and jitter.
6. **Live output.** Agents stream deploy output every 2 s and the job page polls it, so
   you watch a deploy happen across N servers. Output is capped at 256 KiB per command,
   keeping the *tail*, because that's where the error is.
7. **Tokens.** Each server gets its own 256-bit token. It is shown once and stored as
   SHA-256. Rotating it kills the old one immediately. The agent refuses `http://`
   unless you explicitly allow it for local dev.
8. **Audit everything.** Logins, failed logins, server changes, token rotations, every
   job (with its target servers and any skipped ones) and cancellations. The audit log
   is append-only, including in the admin.

Things I left out of v1 on purpose: Prometheus/Loki/Grafana (add them once you outgrow
roughly 200 servers or need log search), WebSockets (2–10 s HTMX polling is enough here
and much simpler to run), and Ansible (the agent's named actions cover "run my deploy
script everywhere").

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
- **Alerts:** offline, CPU, memory, disk, service down. Acknowledge, resolve
  automatically, notify via webhook.
- **Roles:** Viewer (read), Operator (run actions, ack alerts), Admin (manage servers
  and tokens, shell, audit log). Defined in `apps/users/roles.py` and synced on every migrate.

## Quick start (development)

```bash
make install                    # venv + all deps
cp main/.env.example main/.env  # set SECRET_KEY; DEBUG=True for dev
make migrate superuser
make run                        # http://127.0.0.1:8000
make sweep                      # second terminal: offline detection without Celery
```

Add a server in the dashboard and copy the token. To run an agent on your laptop
against the dev server, create `agent/dev.toml`:

```toml
[main]
url = "http://127.0.0.1:8000"
token = "smk_…"
allow_insecure_http = true

[logs]
syslog = "/var/log/system.log"

[actions.disk_report]
command = ["df", "-h"]
```

…and run `make agent-dev`. `python agent/server_monitor_agent.py -c agent/dev.toml --print`
prints the report the agent would send.

## Installing the agent on a real server

The "Add server" page gives you the exact command:

```bash
curl -fsSL https://monitor.example.com/agent/files/install.sh \
  | sudo SM_URL=https://monitor.example.com SM_TOKEN=smk_… bash
```

The installer creates a `server-monitor` system user, a venv in
`/opt/server-monitor-agent`, `/etc/server-monitor-agent/agent.toml` (mode 640) and a
systemd unit. Then declare the server's services, logs and deploy scripts in
`agent.toml`. [`agent/agent.example.toml`](agent/agent.example.toml) documents every
option. For restart buttons, grant only those units in sudoers:

```
server-monitor ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx, /usr/bin/systemctl restart gunicorn-api
```

Re-running the installer upgrades the agent and keeps the config.

## Production

`docker compose up --build` starts Postgres, Redis, web (Gunicorn) and one worker with
beat. Run **exactly one** beat, or sweeps run twice. Put TLS in front (nginx/Caddy) and
set `MONITOR_PUBLIC_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `ALERT_WEBHOOK_URL`.
With `DEBUG=False` and no broker, the app refuses to boot rather than silently running
without offline detection.

Sizing: each heartbeat is about 6 queries (the test suite caps it at 12). 100 servers
at 30 s is about 3 req/s, which is trivial. Metric samples are pruned after 14 days. Past
a few hundred servers, move samples to TimescaleDB or a rollup table, or ship metrics to
Prometheus and keep this app as the control plane.

## Checks

```bash
make test    # 42 main tests + 12 agent tests
make lint    # ruff
make check   # system checks, migrations up to date, check --deploy
```

Tests run on SQLite, where `SELECT FOR UPDATE` is ignored. Run them against Postgres
(`DATABASE_URL=postgres://…`) in CI so the locking path is exercised.

## Layout

```
main/
  config/            settings (env-driven), urls, celery
  apps/fleet/        Server, ServerGroup, MetricSample · ingestion · dashboard + server views
  apps/agent_api/    agent HTTP API + payload validation
  apps/operations/   Job (fan-out) + Command (per server) lifecycle
  apps/alerts/       rules, offline sweep, webhook notifications
  apps/audit/        append-only audit trail
  apps/users/        custom User, roles matrix, throttled login
  templates/         Bootstrap 5 + HTMX, Django 6 template partials for live regions
agent/
  server_monitor_agent.py   the whole agent (stdlib + psutil)
  agent.example.toml        annotated allowlist config
  install.sh                installer / upgrader
  server-monitor-agent.service
```
