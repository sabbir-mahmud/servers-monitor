# Architecture

## Components

| Component | Where | What it is |
|---|---|---|
| **web** | `main/` | Django 6.1 UI with server-rendered templates and HTMX (no JavaScript framework), a token-authenticated JSON API (`/api/v1/`, `/metrics`) and an installable web app (service worker for the offline page and push alerts). |
| **relay process** | `main/apps/agent_api/bridge.py` (`manage.py relay`) | Threads for receiving agent messages, scheduling (job outbox, offline sweep, pruning), application health checks, and notifications. Replaces Celery. |
| **SQLite** | one file, WAL mode | Servers, metrics, jobs, alerts, monitors, users, audit log. |
| **Redis relay** | Upstash or any Redis 6+ | Two lists per server. Carries messages only; holds no long-term state. |
| **agent** | `agent/server_monitor_agent.py` | Runs on each server. Python 3.11+ standard library plus `psutil`, including a built-in Redis client. |
| **smctl** | `agent/smctl.py` | The on-server management command (standard library only). |

## The relay protocol

Every server has two Redis lists:

```
sm:a:<server id>:cmd   dashboard → agent   cmd, cancel, cfg
sm:a:<server id>:in    agent → dashboard   hb (heartbeat), ack, res (result/output), ping
```

- The agent blocks on `BLPOP …:cmd` for up to two minutes, sending a heartbeat (`RPUSH …:in`)
  between waits. Jobs therefore start within a second, while an idle agent costs about two
  Redis commands per heartbeat.
- The relay process `BLPOP`s every enabled server's `…:in` at once, so one blocking call
  covers the whole fleet.
- Each agent authenticates as Redis ACL user `sm-<server id>`, allowed only `RPUSH`, `BLPOP`,
  `LTRIM` and `PING` on its own two keys. Upstash requires these passwords to come from its
  `ACL GENTOKEN` extension; on other Redis servers the dashboard generates them itself.
- Every message is JSON, signed with HMAC-SHA256 under a per-server key that Redis never sees
  (see [security.md](security.md#signed-commands)). The signed form is canonical (sorted keys,
  no spaces), so a queued command can still be found again with `LREM`.

Message types:

| Type | Direction | Fields |
|---|---|---|
| `cmd` | → agent | `id`, `action`, `args`, `timeout`, `exp` (Unix time after which it must not run) |
| `cancel` | → agent | `id`: stop it (lost or cancelled while running) |
| `cfg` | → agent | `report_interval` (pushed when you change it); `probes` + `probes_rev` (uptime checks to run, only to agents with probes on, only when they change) |
| `hb` | → dashboard | `interval`, `report` (host, metrics, disks, services, capabilities, inventory; `probe_results` and `probes_rev` from agents with probes on) |
| `ack` | → dashboard | `id`: popped, starting now |
| `res` | → dashboard | `id`, `status` (`running`/`succeeded`/`failed`), `output` chunk, `exit_code` |
| `ping` | → dashboard | `name`, `status` (`start`/`ok`/`fail`), `exit`: a cron job's heartbeat (`smctl run`/`smctl ping`) |

**Compatibility.** Agents advertise what they can do in `capabilities.features`
(`job_ping`, `inventory`, `log_follow`, `probes`), and the dashboard only offers and sends
what an agent advertises; an older agent never receives a message it can't handle. New
message types and fields are added, never repurposed.

## Job lifecycle

```
            queue                  agent pops + acks              final result
 pending ───────────▶ (in mailbox) ─────────────────▶ running ───────────────▶ succeeded / failed
    │                     │                              │
    │ not delivered       │ cancel: LREM proves the      │ silent past its timeout + 120 s
    ▼ within 5 min        ▼ agent never saw it           ▼
 expired              cancelled                         lost  (never re-sent)
```

- **At most once.** A job is popped by exactly one `BLPOP`. A job that goes silent becomes
  **lost** and is never re-sent: a deploy that runs twice is worse than one you click again.
- **Outbox.** If Redis is unreachable when you click **Run**, the job stays in the database
  with `dispatched_at` empty, and the relay process retries every few seconds.
- **Expiry.** Jobs carry `exp`. The agent refuses expired ones, and the dashboard removes them
  from the mailbox, so an offline server never runs a stale deploy when it comes back.
- **Output** streams every 3 seconds in chunks of up to 60 KiB. The dashboard keeps the last
  256 KiB, because the end of a log is where the error is.
- **Fan-out.** Running an action on many servers creates one job with one command per
  server. Servers that can't run it (not advertised by their agent) are skipped, with the
  reason recorded.
- **Rolling jobs.** A rolling job queues only its first batch; the others wait as
  `waiting`. When a batch has finished, the relay process pauses, then waits for the chosen
  health-gate monitors to pass a check made *after* the batch, then releases the next batch.
  A failure (or a gate that stays down past its timeout) halts the job; **Resume** accepts
  the failures so far and continues, **Abort** cancels the batches not started. Both are
  audited.
- **Schedules** run an action on a 5-field cron expression in `TIME_ZONE` (DST-safe). The
  relay process claims each slot before running it, so a slot runs once even with two relay
  processes, and slots missed while it was down run once, not once per missed slot.
- **Live log follow** (`tail_follow`) is a long-running command: the agent streams new lines
  (of a declared log file, through rotation, or of a declared systemd unit's journal) with
  the usual 3-second flushes for up to 10 minutes; **Stop** sends `cancel`.

## Health

Health is derived from `last_seen_at` when it's read, so nothing has to fire for it to be
right:

- **Online** while the last heartbeat is within 2 intervals + 10 s,
- **Late** within 6 intervals + 10 s,
- **Offline** after that.

The relay process sweeps every 15 seconds to open **Offline** alerts on the transition and to
expire or lose stale jobs.

## Alerts and notifications

- **Rules** run inside heartbeat ingestion (CPU on a 3-sample average, memory, disk, services)
  and inside application checks (down after N consecutive failures, certificate expiring).
  Thresholds have 5 % hysteresis.
- **Deduplication** is a database constraint: one open alert per (server, kind, subject) or
  (monitor, kind).
- **Delivery** is an outbox. Every 5 seconds the relay process works out what each enabled
  channel still owes, from the alerts and the `Delivery` log: "opened" for new matching alerts,
  "resolved" when they close, and reminders while they stay unacknowledged. Failures retry
  with backoff (30 s, 1 m, 2 m, 4 m). Opened and resolved messages are unique per channel, so
  restarts never send duplicates.
- **Maintenance windows** hold notifications for covered servers and apps (a muted server
  mutes its apps too). Alerts still open when the window ends are sent then, marked "held
  during maintenance".

## Application health checks

The checker thread picks due monitors every second and runs them on a pool of 8 workers.
A failed check is retried after 15 seconds instead of a full interval, and the monitor is
only marked down after `failure_threshold` consecutive failures. Results are stored per
check, which powers uptime percentages, the 24-hour bar, response-time charts and the
incident list. Outbound requests go through the network guard (`apps/core/netguard.py`).

**Several locations.** An HTTP or TCP monitor can also be checked by chosen agents (with
`[probes] enabled`). Each agent gets its assignments in a signed `cfg` message, runs them at
most once per heartbeat interval with its own guard (private addresses allowed, cloud
metadata and link-local never), and sends the results with its next heartbeat, so probes
cost **no extra Redis commands**. The dashboard keeps each location's latest result and
stores two kinds of rows: each location's own check, and the monitor's outcome after the
**quorum** (down only when at least *quorum* live locations have failed `failure_threshold`
times in a row; locations silent for 3 intervals, or 10 minutes, don't count). Uptime, bars
and incidents use the outcome rows.

## Long-term history

Raw samples and checks are kept 14 days. Every hour the relay process rolls up each complete
hour per server (average and peak CPU, memory, disk, load, plus a per-mount disk snapshot)
and each complete day per monitor (checks up, total, average response), and keeps them
400 days (`ROLLUP_RETENTION_DAYS`). Charts up to 24 hours read raw samples; 7, 30 and 90
days read rollups (plus raw samples for the hour in progress). The disk forecast fits each
mount's last week of hourly snapshots and the fullest disk's samples, and reports whichever
fills first.

## Repository layout

```
main/                      the dashboard (Django project)
  config/                  settings (environment-driven), URLs, WSGI
  apps/agent_api/          relay: Redis ACL provisioning, signing, the bridge process
  apps/fleet/              servers, groups, metrics, ingestion, server pages, agent checksums
  apps/operations/         jobs and commands: fan-out, dispatch, ack, results, cancel, expiry
  apps/monitors/           application health checks, stats, pages
  apps/alerts/             alert rules, notification channels and delivery, maintenance
  apps/users/              accounts, roles, invitations, password resets, throttling, 2FA, SSO
  apps/api/                REST API tokens and views, Prometheus export
  apps/status/             public status pages (static HTML published to GitHub)
  apps/audit/              append-only audit log
  apps/core/               shared middleware, template tags, crypto, network guard, system
                           page, watchdog, backups, the web app (manifest, service worker)
  templates/, static/      server-rendered UI (Bootstrap base + a custom design system)
  tests/                   dashboard tests (pytest)
agent/
  server_monitor_agent.py  the agent
  smctl.py                 on-server management command
  install.sh               verified installer and upgrader
  server-monitor-agent.service, agent.example.toml
  tests/                   agent and smctl tests
deploy/                    systemd units for running the dashboard without Docker
docs/                      these guides
```
