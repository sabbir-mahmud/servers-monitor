<div align="center">

# Server Monitor

**Monitor, operate and deploy to all your servers from one calm dashboard,
without opening a single inbound port.**

Live health · containers and processes · uptime from many places · status pages ·
rolling and scheduled deploys · alerts everywhere · SSO, 2FA and an API · full audit trail

[Quick start](#quick-start) · [Features](#features) · [How it works](#how-it-works) ·
[Docs](#documentation) · [Security](docs/security.md) · [Contributing](CONTRIBUTING.md)

![License](https://img.shields.io/badge/license-Apache%202.0-blue)
![Python](https://img.shields.io/badge/dashboard-Python%203.14%20%C2%B7%20Django%206.1-3b62d9)
![Agent](https://img.shields.io/badge/agent-Python%203.11%2B%20%C2%B7%20stdlib%20%2B%20psutil-2f9e5b)

</div>

---

## Why Server Monitor?

Most self-hosted monitoring makes you choose: either your dashboard is reachable from the
internet, or every server needs SSH keys, VPNs and open ports. Server Monitor needs neither.

- **No inbound access, anywhere.** The dashboard and every agent dial *out* to a Redis relay
  (Upstash's free tier works) and meet there. Run the dashboard on a laptop, a Raspberry Pi
  at home, or a VPS; servers behind NAT and firewalls just work.
- **The server decides what it allows.** Each agent has a local allowlist of services, logs
  and deploy actions. The dashboard can only *ask*; a compromised dashboard can't turn into
  root on your fleet.
- **Lightweight by design.** Django + SQLite + one small background process, and a handful of
  runtime dependencies on the dashboard; the agent is one Python file plus `psutil`.
- **Secure by default.** Signed commands, verified installs, invite-only accounts, encrypted
  secrets, SSRF guards and an append-only audit log. See the [security model](docs/security.md).

## Features

| | |
|---|---|
| **Fleet dashboard** | CPU, memory, disk, load, network and uptime for every server, refreshed live, with "disk fills in ~6 days" forecasts per mount. Filter by environment, group or health. |
| **Server pages** | Ring gauges, services with state and version (systemd, Docker, pm2, process, HTTP), containers, top processes, temperatures and GPUs, disks, 1 hour to 90 day charts. |
| **One-click operations** | Run allowlisted actions on one server or many, **in rolling batches with health gates**, or **on a cron schedule**, and watch the output stream in. Restart services, tail or **follow logs live** (files and systemd journals), optional audited shell. |
| **Application monitors** | HTTP(S) (POST, headers, keywords, slow thresholds), TCP, ping, DNS and domain-expiry checks, **from the dashboard and from your own servers** with a quorum, plus **Heartbeat** monitors for cron jobs and backups. Uptime bars, response-time charts, incidents, certificate expiry. |
| **Status pages** | Public pages with uptime history and incident updates, published as static files to GitHub Pages or Cloudflare Pages. |
| **Alerts** | Offline, CPU, memory, disk, disk forecast, service, container, temperature, app down, certificate and domain expiring. Deduplicated, with hysteresis so nothing flaps. |
| **Notifications** | Telegram, email, Slack, Discord, ntfy, Gotify, Pushover, PagerDuty, webhooks and **browser push**, with routing rules, reminders until acknowledged, retries and a delivery log. |
| **Maintenance windows** | Schedule a window or press **Mute** on any server or app: alerts are still recorded, nobody gets paged. |
| **Teams** | Viewer / Operator / Admin roles, invitations or **single sign-on (OIDC)** with group-to-role mapping, **two-factor sign-in**, self-service or admin password resets. |
| **Integrations** | A token-authenticated **REST API** and **Prometheus `/metrics`**; an installable web app. |
| **Trust the system** | A watchdog for the dashboard itself, daily backups (plus optional Litestream), a Redis usage meter and a System page. |
| **`smctl`** | One command on each server to detect services, add deploy actions, manage sudo rules, wrap cron jobs, diagnose problems and upgrade. |

## How it works

```
   your machine (no open ports)          Redis relay (TLS)            your servers (no open ports)
 ┌──────────────────────────────┐     ┌───────────────────┐      ┌───────────────────────────┐
 │ web    Django UI             │     │ sm:a:<id>:cmd  ◀──┼─push─│                           │
 │ relay  receives heartbeats,  │────▶│ sm:a:<id>:in   ───┼─pop──│ agent  (Redis user that   │
 │        sends jobs, checks    │     │   one pair of     │      │         can only touch    │
 │        apps, notifies        │     │   keys per server │      │         its own two keys) │
 │ SQLite servers, jobs, audit  │     └───────────────────┘      └───────────────────────────┘
 └──────────────────────────────┘
        dials out ─────────────▶         ◀───────────────── dials out
```

1. **Add a server** in the dashboard. It creates a Redis user that can only reach that server's
   two mailbox keys, plus a signing key that never touches Redis.
2. **Run one verified command** on the server. It installs the agent from this repository,
   checks every file against checksums from your dashboard, and connects.
3. **The agent reports** every two minutes and waits on its mailbox, so a deploy you click
   starts within a second. Output streams back as it runs.

Deep dive: [docs/architecture.md](docs/architecture.md).

## Quick start

You need **Docker** and a **Redis with TLS and ACL support**. A free
[Upstash](https://upstash.com) database is the easiest option.

```bash
git clone https://github.com/sabbir-mahmud/servers-monitor.git
cd servers-monitor
cp main/.env.example main/.env
```

Edit `main/.env` and set at least:

```ini
SECRET_KEY=<python3 -c 'import secrets; print(secrets.token_urlsafe(50))'>
REDIS_URL=rediss://default:<password>@<name>.upstash.io:6379
HTTPS=False          # serving on this machine over plain HTTP; see docs/deployment.md
TIME_ZONE=UTC        # e.g. Asia/Dhaka, Europe/London
```

Start it and create the first administrator:

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Open **http://127.0.0.1:8000**, go to **Servers → Add server**, and follow the two-step
instructions on the page. The server appears as **Connected** within seconds.

Not using Docker, running on a Raspberry Pi, or putting HTTPS in front? See
[docs/deployment.md](docs/deployment.md).

## Managing a server with `smctl`

The installer adds `smctl` to every server. The server's allowlist lives in
`/etc/server-monitor-agent/agent.toml`; `smctl` edits it safely, validates every change
with the agent itself, and applies it immediately.

```bash
sudo smctl status                                  # running? connected?
sudo smctl doctor                                  # diagnose config, relay, permissions
sudo smctl detect --apply                          # add services and logs found on this host
sudo smctl action add deploy -d "Pull and restart" -- /opt/deploy/deploy.sh
sudo smctl service restartable nginx on            # Restart button, with an exact sudo rule
sudo smctl log add app /var/www/app/logs/app.log   # make a log tail-able from the dashboard
```

Full reference: [docs/agent.md](docs/agent.md).

## Documentation

| Guide | What's inside |
|---|---|
| [Deployment](docs/deployment.md) | Docker, systemd and Raspberry Pi installs, HTTPS options, backups, upgrades, Redis cost |
| [Agent & smctl](docs/agent.md) | Installing on servers, the allowlist, every `smctl` command, upgrades, troubleshooting |
| [Configuration](docs/configuration.md) | Every environment variable with its default, including single sign-on |
| [REST API](docs/api.md) | Tokens, endpoints, running actions from CI, Prometheus scraping |
| [Architecture](docs/architecture.md) | The relay protocol, job lifecycle, health model, alerting and notifications |
| [Security model](docs/security.md) | Threat model, signed commands, verified installs, hardening checklist |

## Requirements

| Component | Needs |
|---|---|
| Dashboard | Docker, or Python 3.14 on any Linux/macOS box (≈ 200 MB RAM) |
| Relay | Redis 6+ with TLS and `ACL SETUSER` (Upstash, self-hosted Redis or Valkey) |
| Monitored servers | Linux with systemd and Python 3.11+ (Ubuntu 22.04+, Debian 12+, RHEL 9+) |

## Development

```bash
make install          # virtualenv + dependencies
cp main/.env.example main/.env   # set DEBUG=True, SECRET_KEY and REDIS_URL
make migrate superuser
make dev              # dashboard on :8000 + the relay process, in one terminal
make test             # dashboard and agent test suites
make lint             # ruff (the agent is held to Python 3.11 syntax)
make check            # Django system checks, migrations, production deployment audit
```

The tests run without Redis or network access: the relay is exercised through an
in-memory fake, and HTTP checks, Telegram and Redis wire formats against local test servers.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the project layout and conventions.

## Contributing

Bug reports, ideas and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
first. **Found a security issue? Please don't open a public issue:** follow
[SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
Copyright © 2026 Sabbir Mahmud and contributors.
