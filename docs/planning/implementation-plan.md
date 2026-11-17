# Implementation plan

Turns the roadmap in [plan.md](plan.md) into buildable work. Each item says what changes in
the codebase, the key design decisions, the tests that prove it, and when it counts as done.

*Estimates are focused developer-days for one person who knows the codebase, including
tests and docs.*

---

## Contents

- [Ground rules for every item](#ground-rules-for-every-item)
- [Overview and sequencing](#overview-and-sequencing)
- [Phase 0: release engineering](#phase-0-release-engineering) (3 days)
- [Phase 1: trust the system](#phase-1-trust-the-system) (10 days)
- [Phase 2: catch what's missed](#phase-2-catch-whats-missed) (17 days)
- [Phase 3: operate at scale](#phase-3-operate-at-scale) (19 days)
- [Phase 4: platform](#phase-4-platform) (16 days)
- [Cross-cutting concerns](#cross-cutting-concerns)
- [Risk register](#risk-register)

---

## Ground rules for every item

These hold for every change. Pull requests that miss one aren't done.

1. **No new inbound surface.** Nothing may require the dashboard or a server to accept
   connections. New data paths go through the relay or outbound HTTPS.
2. **Security parity.** Relay messages are signed (`apps/agent_api/signing.py` and the agent's
   `sign`/`verify`). New outbound URLs typed by users go through `apps/core/netguard.py`. New
   secrets use `EncryptedTextField`. New actions are audited (`apps/audit/services.record`).
3. **Agent stays lean.** Python 3.11 syntax, standard library plus `psutil` only, and anything
   that runs on a server is opt-in through `agent.toml` and `smctl`.
4. **Redis budget.** Each item states its Redis command cost. Default target: a monitored
   server stays near today's ~65K commands per month.
5. **Tests first-class.** Behaviour tests in `main/tests/` or `agent/tests/`. Security-relevant
   changes add an attack-and-fail test to `main/tests/test_security.py`.
6. **Docs in the same PR.** Update `README.md`, the relevant `docs/*.md` and
   `docs/configuration.md` for new settings.
7. **Migrations are forward-safe.** New columns nullable or defaulted, data migrations
   idempotent, and `make check` passes.
8. **Agent protocol compatibility.** The dashboard keeps working with agents one minor version
   behind. New agent capabilities are advertised in the heartbeat (`capabilities`) and the
   dashboard gates features on them.

---

## Overview and sequencing

| Phase | Item | Plan ref | Effort | Depends on |
|---|---|---|---|---|
| **0** | 0.1 CI pipeline | n/a | 1 d | |
| | 0.2 Versioning, changelog, tagged releases | n/a | 2 d | 0.1 |
| **1** | 1.1 System page + dashboard watchdog | #1 | 2.5 d | |
| | 1.2 Backups | #2 | 2 d | 1.1 |
| | 1.3 Two-factor login | #3 | 3.5 d | |
| | 1.4 Redis usage meter | #10 | 1 d | 1.1 |
| | 1.5 Disk-full forecast | #5 | 1 d | |
| **2** | 2.0 Monitor kinds refactor | #6, #7 | 2 d | |
| | 2.1 Heartbeat (cron) monitors | #6 | 3 d | 2.0 |
| | 2.2 New check types | #7 | 4 d | 2.0 |
| | 2.3 Unified channel config + new channels | #8 | 4 d | |
| | 2.4 Public status pages | #4 | 4 d | 2.0 |
| **3** | 3.1 Containers, processes, temperatures | #11 | 4 d | |
| | 3.2 Scheduled and rolling jobs | #12 | 6 d | |
| | 3.3 Agent upgrades from the dashboard | #9 | 4 d | 0.2 |
| | 3.4 Agents as uptime probes | #13 | 5 d | 2.0 |
| **4** | 4.1 REST API + tokens, Prometheus export | #14 | 5 d | |
| | 4.2 Long-term rollups | #15 | 3 d | |
| | 4.3 Live log follow | #16 | 3 d | |
| | 4.4 PWA + push | #17 | 2 d | |
| | 4.5 SSO (OIDC) | #18 | 3 d | 1.3 |

**Total: about 65 developer-days.** Phases 0–2 (about 30 days) deliver the "about 4th in pure
monitoring" outcome from `plan.md`.

Suggested release cadence: one minor release per phase (`v2.2` = Phase 0+1, `v2.3` = 2,
`v2.4` = 3, `v2.5` = 4), with patch releases in between.

---

## Phase 0: release engineering

Verified installs (`apps/fleet/agentdist.py`) compare GitHub's agent files with the dashboard's,
so releases must be tagged and reproducible before features ship. CI also has to enforce the
ground rules.

### 0.1 CI pipeline (1 d)

- **Add** `.github/workflows/ci.yml`, running on push and PR:
  `make install` → `make lint` → `make test` → `make check`, on Python 3.14 for the dashboard.
- **Agent matrix job:** run `agent/tests` on Python **3.11, 3.12 and 3.13** using
  `actions/setup-python`. This replaces the grammar-only guard with real interpreters.
- **Shell:** `shellcheck agent/install.sh`.
- **Docker:** `docker build -f main/Dockerfile .` (build only).
- **Done when:** a PR that breaks lint, tests, migrations, the 3.11 agent or the installer fails.

### 0.2 Versioning, changelog, tagged releases (2 d)

- **One version source:** `VERSION` file at the repo root, read by Django
  (`settings.APP_VERSION`, shown in the System page and footer) and by
  `agent/server_monitor_agent.py`. A test asserts they agree.
- **Changelog:** `CHANGELOG.md` ("Keep a Changelog" format). Every PR adds a line under
  *Unreleased*, and agent-file changes are flagged **"agents must be upgraded"**.
- **Release workflow** `.github/workflows/release.yml` on tag `v*`:
  1. run CI,
  2. compute `SHA256SUMS` for `agent/*`, attach it to the GitHub Release,
  3. build and push a container image `ghcr.io/sabbir-mahmud/servers-monitor:<tag>`, also `:latest`.
- **Defaults:** the image sets `AGENT_REF=<tag>`, so a released dashboard installs exactly its
  own agent code.
- **Docs:** the deployment guide recommends the GHCR image for production.
- **Done when:** tagging `v2.2.0` produces a release with checksums and an image whose install
  commands pin `v2.2.0`.

---

## Phase 1: trust the system

### 1.1 System page + dashboard watchdog (2.5 d)

**Goal:** solve "who watches the watcher", and give Admins one place to see system health.

**Design**
- **New setting** `WATCHDOG_URL` (e.g. a Healthchecks.io ping URL). The relay process pings it
  every 60 s through `netguard.urlopen` from a new `watchdog` thread in `Bridge.run`. Failures
  are logged, never fatal.
- **Gap detection:** when the relay starts, it reads the `.relay-alive` marker (already written
  every 5 s by `relay.mark_alive`). If the gap is over 5 minutes, it:
  - writes an audit row `system.downtime` (`from`, `to`, `minutes`),
  - sends a system notification to every enabled channel: *"Server Monitor was offline from
    02:10 to 04:31 (2 h 21 m). Health checks and alerts during that time were missed."*
- **System notifications** are a new path in `apps/alerts/delivery.py`: `send_system(text)`
  calls `send()` per enabled channel and records a `Delivery(event="system", alert=None)`.
  It adds `DeliveryEvent.SYSTEM`.
- **System page** `/system/` (new `apps/core/views.py`, permission `users.view_user`, sidebar
  *Administration → System*), with tiles for:
  - relay process (alive, last seen),
  - watchdog (configured, last ping result),
  - backups (from 1.2),
  - Redis usage (from 1.4),
  - app and agent versions (from 0.2),
  - database size,
  - email enabled,
  - HTTPS mode.

**Data:** none beyond a small `SystemState` key-value model (`key`, `value` JSON, `updated_at`)
for the watchdog's last result and the last downtime. One migration.

**Redis cost:** none (HTTPS only).

**Tests:** watchdog pings a local test server and records success or failure. Gap detection
sends exactly one system notification and one audit row. No gap means no message. The page
needs Admin.

**Done when:** stopping the relay for 6 minutes then starting it sends a Telegram message, and
the Healthchecks.io check goes red within its grace time while the relay is stopped.

### 1.2 Backups (2 d)

**Goal:** never lose history or config to one disk failure.

**Design**
- **Local snapshots (always on):** the relay scheduler takes a daily `sqlite3` online backup
  (`Connection.backup`) to `BACKUP_DIR` (default next to `SQLITE_PATH`, `/data/backups` in
  Docker). It keeps `BACKUP_KEEP` (default 14) and writes the result to `SystemState`.
- **Off-site, continuous (optional):** a `litestream` service in `docker-compose.yml` (profile
  `backup`), with `deploy/litestream.yml` replicating `/data/db.sqlite3` to any S3-compatible
  bucket (Cloudflare R2, Backblaze B2, S3). Config comes from env `LITESTREAM_*`.
- **Commands:** `manage.py backup` (snapshot now) and `manage.py restore <file>` (refuses while
  the relay marker is fresh; validates the file with `PRAGMA integrity_check`).
- **Status:** the System page shows last snapshot, size and age; it warns when the latest is
  over 36 hours old.
- **Docs:** `docs/deployment.md` "Backups" rewritten around this, with a restore walkthrough.
  It must say `main/.env` is needed too.

**Redis cost:** none.

**Tests:** a snapshot is a valid SQLite file with identical row counts; rotation keeps N;
restore refuses while running; a stale backup shows a warning.

**Done when:** `docker compose --profile backup up -d` replicates to R2, and a fresh machine
restores from it with documented steps.

### 1.3 Two-factor login (3.5 d)

**Goal:** a stolen password alone can't reach a dashboard that deploys to servers.

**Design**
- **TOTP (RFC 6238):** SHA-1, 6 digits, 30 s, ±1 step, in `apps/users/totp.py` using the
  standard library, tested against the RFC's own test vectors.
- **New dependency** `segno` (pure Python, no dependencies) to render the setup QR code as SVG
  server-side. The CSP stays script-free for this.
- **Models:**
  - `TOTPDevice`: `user` one-to-one, `secret` as `EncryptedTextField`, `confirmed_at`,
    `last_used_step` (blocks code replay within the window).
  - `RecoveryCode`: `user`, `code_hash` (SHA-256), `used_at`. 10 codes, shown once.
- **Login flow:** `ThrottledLoginView.form_valid`:
  - with 2FA on, it stores `pending_2fa_user` plus a timestamp in the session (5-minute
    expiry) and redirects to `/login/verify/`,
  - otherwise it signs in as today.
- **Verification:** `VerifyView` accepts a TOTP or a recovery code, throttled through
  `throttle.allow("2fa", …)` at 5 attempts per 15 minutes per user, then completes `login()`.
- **Enrolment:** *My account → Two-factor* covers setup (QR, manual key, confirm with a code),
  regenerating recovery codes and disabling (needs password and a code).
- **Enforcement:** setting `REQUIRE_2FA_FOR_ADMINS` (default `True`). Middleware redirects
  Admins without a confirmed device to setup, except for the setup, logout and static URLs.
- **Recovery:** an Admin can reset another user's 2FA on the Users page (audited). Superusers
  are protected by `check_can_manage`.
- **Django admin:** already routed through the dashboard login (finding #2), so it inherits 2FA.

**Tests:**
- RFC vectors, ±1 window and code replay rejected.
- Login without the second step can't reach any page; the pending session expires.
- Recovery codes are single-use; the throttle locks.
- Admins are forced into setup; disabling needs password and code; Admin reset is audited.
- A security test: password-only access fails.

**Done when:** an Admin can't use the dashboard without a second factor, and every path above
is tested.

### 1.4 Redis usage meter (1 d)

**Goal:** no surprise Upstash bills or free-tier cut-offs.

**Design**
- **Count the dashboard's own commands:** wrap the client returned by `relay.client()` in a thin
  counter (`execute_command` hook) that increments an in-memory counter, flushed to
  `RelayUsage(date, dashboard_commands, agent_commands_estimated)` every minute by the relay
  scheduler.
- **Estimate agent-side commands from what arrives:** each heartbeat implies about 2 agent
  commands (RPUSH + BLPOP), each result chunk or ack 1, plus LTRIM every 50 sends.
- **System tile:**
  - this month so far,
  - projected month-end total,
  - estimated cost at `RELAY_PRICE_PER_100K` (default `0.2`) above `RELAY_FREE_COMMANDS`
    (default `500000`),
  - a warning above 80 % of the free tier.
- **Later, optional:** read exact numbers from the Upstash developer API when
  `UPSTASH_API_KEY` is set.

**Tests:** the counter increments per call, the daily rollup persists, and the projection maths
is right.

**Done when:** the System page shows a month projection within about 15 % of Upstash's console
for a test week.

### 1.5 Disk-full forecast (1 d)

**Goal:** turn the most common outage into a planned task.

**Design**
- **Hourly job** in the relay scheduler: per server, take hourly averages of
  `MetricSample.disk_percent` over 7 days and fit a least-squares line.
- **Store** `Server.disk_full_at` (nullable) when:
  - the slope is above 0.05 %/h,
  - R² is at least 0.6,
  - and there are at least 48 points.
- **Alert:** new `AlertKind.DISK_FORECAST`, warning when within 7 days and critical within 2.
  It resolves when the forecast moves past 10 days or disappears (hysteresis).
- **UI:** "fills in about 6 days" hint on the server page's fullest-disk ring and in the
  dashboard table.
- **Limitation (documented):** history exists for the *fullest* disk only, not each mount.
  Per-mount history comes with rollups (4.2).

**Redis cost:** none.

**Tests:** synthetic linear growth predicts within ±5 %; noisy or flat data gives no forecast;
the alert opens and resolves with hysteresis.

**Done when:** a server growing about 1 %/day at 90 % shows "fills in about 10 days" and alerts
at 7.

---

## Phase 2: catch what's missed

### 2.0 Monitor kinds refactor (2 d), foundation for 2.1, 2.2, 2.4

**Design:** `apps/monitors/models.Monitor` gains:
- `kind`: `http` (default, existing rows), `tcp`, `ping`, `dns`, `domain` or `heartbeat`,
- `options` JSON for per-kind settings, validated by a per-kind schema,
- `secret_options` (`EncryptedTextField` holding JSON) for things like request headers.

The `url` field keeps working for `http`.

`checker.probe()` becomes a registry: `PROBES = {"http": probe_http, "tcp": probe_tcp, …}`.
Each probe returns the existing `Result`. `record()`, alerts, stats and every page stay
kind-agnostic.

**Form:** a kind selector at the top; the kind-specific fields swap in with HTMX
(`hx-get` a fields partial).

**Migration:** add the columns with existing rows set to `kind="http"`. No data loss.

**Tests:** existing monitor tests pass unchanged, and the registry dispatches by kind.

### 2.1 Heartbeat (cron) monitors (3 d)

**Goal:** catch silent cron and backup failures.

**Design (no public endpoint):**
- **Monitor:** `kind="heartbeat"` with options `period_seconds`, `grace_seconds`, and a `server`
  (required: the server that pings).
- **Sending a ping:** `sudo smctl ping NAME [--fail] [--start] [--exit-code N]`. `smctl` runs as
  root, reads `agent.toml`, and RPUSHes a **signed**
  `{"t":"ping","name":…,"status":"ok|fail|start","exit":…,"ts":…}` to the server's inbox key,
  using the server's own Redis credentials.
- **Wrapper:** `smctl run NAME -- CMD…` runs a command and pings start, then ok or fail with
  its exit code. That makes `0 3 * * * smctl run backup -- /opt/backup.sh` work in crontab.
- **Receiving:** the bridge's `handle()` gets a `ping` branch that finds the matching
  `Monitor(kind=heartbeat, server=…, name=…)` and records an ok or failed check.
- **Missed pings:** the checker treats a heartbeat monitor as due at
  `last_ping + period + grace` and records a failed check ("no ping since 03:00") when it
  passes.
- **Durations:** a `start` ping followed by `ok` stores the job's runtime, shown in history.

**Redis cost:** 1–3 commands per ping (cron frequency, usually daily); negligible.

**Tests:**
- ping → up; missed → down after grace; `--fail` → down immediately; `run` wrapper exit codes.
- An unsigned or forged ping is ignored, and a ping for another server's monitor is ignored.
- `smctl` ping signing interoperates with the dashboard (cross-implementation test like the
  relay one).

**Done when:** `smctl run nightly-backup -- false` turns the monitor red and sends the alert.

### 2.2 New check types (4 d)

| Kind | Implementation | Notes |
|---|---|---|
| `tcp` | `netguard.create_connection((host, port), timeout)`; success = connected | Databases, SMTP, SSH. Optional TLS handshake and certificate expiry. |
| `ping` | System `ping -c 3 -W timeout` parsed for loss and average RTT | Docker needs `cap_add: [NET_RAW]` on the `relay` service, documented. Targets resolved and checked with `netguard.is_allowed` first. |
| `dns` | Record type A, AAAA, CNAME, MX or TXT; expected value contains, or changed since the last check | New dependency `dnspython` (pure Python), needed for MX/TXT. Resolver configurable (default system). |
| `domain` | Expiry date from **RDAP** over HTTPS (`https://rdap.org/domain/<name>`) through `netguard` | No WHOIS parsing. Warn under 30 days, critical under 7. Checked daily. |
| `http` additions | Method (GET/HEAD/POST/PUT), headers, body, "keyword must **not** appear", follow redirects on/off, expected response time (slow = degraded) | Headers stored in `secret_options` (they often carry tokens). |

- **Degraded state:** a slow but successful response is "degraded" (amber in the uptime bar),
  not down. The new `MonitorStatus.DEGRADED` doesn't open a down alert, only an optional
  warning alert after N consecutive slow responses.

**Tests:** each kind against local test servers (TCP listener, a fake DNS via `dnspython`'s
test server or a mocked resolver, a recorded RDAP JSON fixture); netguard blocks still apply;
degraded handling.

**Done when:** you can monitor Postgres on 5432, `example.com` DNS, and a domain's expiry,
and each shows up and down correctly.

### 2.3 Unified channel config + new channels (4 d)

**Goal:** reach people where they are, and page teams properly.

**Design**
- **Refactor `Channel`:**
  - replace `telegram_token`, `telegram_chat_id` and `webhook_url` with `config`
    (`EncryptedTextField` holding JSON) plus a non-secret `target_display`,
  - a data migration moves existing values into it,
  - sending goes through a registry `SENDERS[kind] = Sender(fields, validate, send, send_resolved)`.
- **New kinds:**

| Kind | Config | Notes |
|---|---|---|
| `email` | recipients | Uses `MAILERS`. Only offered when `EMAIL_ENABLED`. |
| `ntfy` | server URL (default `https://ntfy.sh`), topic, optional token | Priority from severity. Self-hosted ntfy on a LAN needs `MONITOR_ALLOW_PRIVATE_TARGETS`. |
| `gotify` | server URL, app token | |
| `pushover` | user key, app token | Emergency priority for critical (needs acknowledgement). |
| `pagerduty` | Events API v2 routing key | `trigger`, then `resolve` with `dedup_key = alert id`: real incident lifecycle. |
| `slack`, `discord` | webhook URL | Native rich formatting (blocks / embeds) instead of plain text. Existing generic webhook kept. |

- **Message formats:** `compose()` returns a small structured message (`title`, `body`, `url`,
  `severity`, `event`), and each sender formats it its own way.
- **Form:** fields per kind, as in 2.0, with a "Send test" button per kind.

**Tests:** every sender against a local fake endpoint asserting its exact payload; the
migration preserves existing Telegram and webhook channels; PagerDuty resolve uses the same
dedup key; secrets are never echoed.

**Done when:** each kind can be added, tested and receives opened and resolved messages.

### 2.4 Public status pages (4 d)

**Goal:** customers see uptime without your dashboard being exposed.

**Design: static publishing (no public endpoint)**
- **Models:**
  - `StatusPage`: `slug`, title, description, logo URL, `monitors` (M2M with per-page display
    name and order), `show_uptime_bars`, `publish_kind` (`github`, `cloudflare`, `download`),
    `publish_config` (encrypted JSON), `last_published_at`, `last_hash`.
  - `StatusIncident`: page, title, status (investigating, identified, monitoring, resolved),
    updates (`StatusIncidentUpdate`: text, time), affected monitors.
- **Rendering:** a Django template renders a **self-contained** `index.html` (inline CSS, no
  JavaScript, `<meta http-equiv="refresh" content="60">`) plus `status.json`. Contents: overall
  state, per-monitor status, a 90-day uptime bar from `MonitorCheck`, and active and recent
  incidents.
- **Publishers:**
  - **GitHub Pages:** the GitHub contents API (`PUT /repos/{repo}/contents/{path}`) with a
    fine-grained token limited to that repo.
  - **Cloudflare Pages:** direct-upload API.
  - **Download:** a ZIP to host anywhere.
- **Scheduling:** the relay republishes when the rendered hash changes, checked every 2
  minutes, and at once when an incident is posted.
- **Privacy:** only selected monitors, with display names; URLs, servers and errors are never
  published.

**Redis cost:** none.

**Tests:** rendering includes only the chosen monitors and no internal URLs; the hash
suppresses unchanged publishes; the GitHub publisher sends the right API calls (mocked);
incident lifecycle.

**Done when:** a page on `status.example.com` (GitHub Pages) updates within about 2 minutes
of an outage and shows the incident timeline.

---

## Phase 3: operate at scale

### 3.1 Containers, top processes, temperatures (4 d)

**Agent** (`Collector`, gated by capabilities):

| Data | How |
|---|---|
| **Containers** | **cgroup v2 files** (`/sys/fs/cgroup/system.slice/docker-*.scope/{cpu.stat,memory.current}`), readable without the docker group. Names via `docker ps` only when `smctl docker enable` was chosen, otherwise short IDs. pm2 apps through the existing `pm2 jlist`. |
| **Top processes** | `psutil.process_iter`: top 10 by CPU and top 10 by memory (name, PID, user, CPU %, RSS, command line truncated to 120 characters). |
| **Temperatures** | `psutil.sensors_temperatures()` (CPU package and NVMe, high and critical marks). |
| **GPU** | `nvidia-smi --query-gpu=… --format=csv` when present. |

**Dashboard**
- **Payload:** `schema.clean_report` gains bounded `containers`, `processes`, `temps` and
  `gpus` sections, with list caps and string truncation, so the heartbeat stays under 32 KB.
- **Storage:** `Server` gets JSON snapshot fields. History per container comes later, with
  rollups.
- **Alerts:** container not running (reuses the SERVICE kind), temperature at or above the
  sensor's high mark (new `AlertKind.TEMPERATURE`).
- **Server page:** new *Containers* and *Processes* sections and a temperature tile.

**Redis cost:** the same message, slightly larger, so effectively none.

**Tests:** collector output shape with mocked psutil and cgroup files; schema caps; alerts;
heartbeats stay under the size budget.

**Done when:** a Docker host shows per-container CPU and memory and top processes without
being added to the docker group.

### 3.2 Scheduled and rolling jobs (6 d)

**Scheduled**
- **Model** `Schedule`: name, action, args, targets (servers M2M, plus groups and environments
  as selectors resolved at run time), `cron`, `timezone`, `enabled`, `next_run_at`,
  `last_job`, `created_by`.
- **Cron parsing:** standard 5-field cron in `apps/operations/cron.py` (standard library,
  tested against a table of expressions), or the `croniter` dependency if edge cases grow.
- **Runs:** the relay scheduler creates a `Job` through `create_job` when `next_run_at` passes,
  so jobs keep the same audit, skip and dispatch path. Missed runs while the relay was down
  run once at most ("catch-up = latest only").

**Rolling**
- **`Job` gains:**
  - `strategy` (`all` or `rolling`),
  - `batch_size`,
  - `pause_seconds`,
  - `stop_on_failure`,
  - `health_gate_monitors` (M2M: wait until these app monitors are up after each batch,
    timeout N minutes),
  - `state` (`running`, `halted`, `completed`).
- **Dispatch:** only batch 1 is dispatched. When every command in the batch is final, the
  scheduler waits `pause_seconds` and checks health gates, then dispatches the next batch or
  halts with a reason.
- **Halting:** stops further batches; running ones finish. **Resume** and **Abort** buttons
  (audited).
- **UI:** the job form gets a *Rollout* section; job detail shows a batch timeline; there's a
  Schedules list and editor.

**Redis cost:** unchanged per command.

**Tests:**
- Cron table and the next-run calculation across DST.
- A schedule fires exactly once per slot, with catch-up after downtime.
- Batch progression, stop on failure, health-gate wait and timeout, resume and abort, and
  audit rows.

**Done when:** "deploy to web-* in batches of 2, stop if the Shop app goes down" works end
to end.

### 3.3 Agent upgrades from the dashboard (4 d)

**Challenge:** the agent runs unprivileged and must not be able to install arbitrary code as
root.

**Design: request file plus a root oneshot service**
- **Units:** `agent/server-monitor-upgrade.path` watches
  `/var/lib/server-monitor-agent/upgrade-request.json`;
  `agent/server-monitor-upgrade.service` (root, oneshot) runs `smctl upgrade --from-request`.
- **The command:** the dashboard sends a **signed** `cmd` with the built-in action
  `_upgrade` and args `{ref, sums}`.
- **The agent:** only writes the request file, and only if `[agent] remote_upgrade = true`
  (`smctl remote-upgrade on`, off by default).
- **`smctl upgrade --from-request`** (root) checks that:
  - `ref` matches `^v\d+\.\d+\.\d+$` (release tags only),
  - the repository is the one recorded at install,
  - the sums are 64-hex values.

  It then runs the existing verified upgrade and reports the result through the relay as the
  job's output.
- **UI:** an *Upgrade agent* action per server and *Upgrade all outdated* (via 3.2 rolling
  jobs). The fleet table shows agent versions behind the dashboard's release.

**Threat note (documented):** a compromised agent user could request any **release tag** of
the official repository with matching checksums. That's no worse than the current trust in
releases.

**Tests:** request validation (bad ref or sums refused), agent refuses when not opted in,
end to end with a mocked installer, rolling fleet upgrade.

**Done when:** "Upgrade all" moves a fleet from v2.2.0 to v2.3.0 in batches with no SSH.

### 3.4 Agents as uptime probes (5 d)

**Goal:** check apps from where your servers are (multi-location, and internal URLs) without
extra infrastructure or Redis cost.

**Design: results ride on heartbeats**
- **Monitor** gains `locations`: `dashboard` (today) and/or chosen servers, plus `quorum`
  (down only if at least N locations fail).
- **Assignments:** the dashboard pushes probe assignments to each chosen agent in the existing
  `cfg` message: `{"probes": [{id, kind, target, options, interval}]}`, signed and re-sent
  when they change.
- **The agent** (`[probes] enabled = true`, opt-in; `smctl probes on`):
  - runs assigned HTTP and TCP probes on its own timer,
  - applies its own netguard (private targets allowed by default on the agent, which is the
    point for internal health URLs; metadata is still blocked),
  - adds results to the next heartbeat as `probe_results`.
- **The dashboard** records per-location `MonitorCheck` rows (a new `location` column) and
  applies the quorum.
- **UI:** per-location rows on the monitor page; the uptime bar uses the quorum outcome.

**Redis cost:** zero extra commands, since results ride existing heartbeats. Probe frequency
is bounded by the heartbeat interval (2 minutes by default); documented.

**Tests:** assignment push and signature; agent probe execution with mocked HTTP; results
ingestion; quorum logic (1 of 3 down = up, 2 of 3 = down); opt-out agents ignored.

**Done when:** an app checked from the dashboard and two servers only alerts when two
locations agree it's down.

---

## Phase 4: platform

| Item | Design summary | Effort |
|---|---|---|
| **4.1 REST API + tokens, Prometheus export** | `ApiToken` (owner, name, SHA-256 hash, scopes `read`/`operate`, expiry, last used). Plain JSON views under `/api/v1/` (servers, monitors, alerts, jobs: list and create), no DRF, CSRF-exempt with token auth only, throttled per token, audited. `/metrics` in Prometheus text format, token-protected. OpenAPI document written by hand in `docs/api.md`. The dashboard still isn't public: the API is for people and tools that can already reach it. | 5 d |
| **4.2 Long-term rollups** | `MetricRollup` (server, hour, avg/max CPU, memory, disk, per-mount disk) built hourly from samples; kept 400 days. Charts use samples up to 24 h and rollups beyond. Enables per-mount disk forecasts (improves 1.5). | 3 d |
| **4.3 Live log follow** | Built-in `tail_follow` action: the agent streams new lines for up to 10 minutes (reusing the streaming output path) with a server-side filter (substring). The UI log viewer gets **Follow** and **Stop**. journald support via `journalctl -u UNIT -f` for declared units. Redis: about 1 command per 3 s while following, shown in the UI. | 3 d |
| **4.4 PWA + push** | `manifest.webmanifest` and a service worker (CSP-compatible, same origin), an offline shell, an "Add to home screen" prompt. Web Push as a notification channel kind (VAPID keys generated per install, via the `pywebpush` dependency). Push needs HTTPS (`HTTPS=True`). | 2 d |
| **4.5 SSO (OIDC)** | `mozilla-django-oidc` (one dependency) behind `OIDC_*` settings. Group claim mapped to role. Local accounts kept for break-glass. 2FA still enforced for local Admin logins. Invitations optional when SSO is on. | 3 d |

---

## Cross-cutting concerns

### Agent protocol versioning
- **Advertising:** the agent sends `agent_version` and a `capabilities.features` list
  (e.g. `["probes", "remote_upgrade", "containers"]`).
- **Gating:** the dashboard gates UI and messages on those features. An older agent never
  receives a `probes` config or a `_upgrade` command it can't handle, and its server page
  says what upgrading unlocks.
- **New message types** (`ping`, `probe_results`) are ignored by older dashboards. Never
  repurpose an existing field.

### Settings to add (all documented in `docs/configuration.md`)
`WATCHDOG_URL`, `BACKUP_DIR`, `BACKUP_KEEP`, `LITESTREAM_*`, `REQUIRE_2FA_FOR_ADMINS`,
`RELAY_FREE_COMMANDS`, `RELAY_PRICE_PER_100K`, `UPSTASH_API_KEY` (optional), `OIDC_*`.

### New dependencies (each justified, all pure Python)
| Dependency | Item | Why not the standard library |
|---|---|---|
| `segno` | 1.3 | QR code rendering |
| `dnspython` | 2.2 | MX/TXT/CNAME lookups |
| `pywebpush` | 4.4 | Web Push encryption |
| `mozilla-django-oidc` | 4.5 | OIDC is easy to get subtly wrong |

The agent gains **no** dependencies in any phase.

### Database growth
| Table | Growth | Control |
|---|---|---|
| `MonitorCheck` | 1 row per check (+ per location in 3.4) | Existing retention (14 d); rollups for long history |
| `RelayUsage` | 1 row per day | Keep 400 days |
| `Delivery` | Per notification | Existing 30-day prune |
| `MetricRollup` | 24 rows per server per day | 400 days ≈ 10K rows per server |

### Performance budget
- Heartbeat ingestion stays at or below 12 queries (an existing test) after 3.1 and 3.4.
- Heartbeat payload stays at or below 32 KB.
- Every new page renders in 200 ms or less with 200 servers and 500 monitors (a new
  benchmark test with seeded data in CI).

---

## Risk register

| Risk | Phase | Likelihood | Mitigation |
|---|---|---|---|
| 2FA lockout of the only Admin | 1 | Medium | Recovery codes; `manage.py reset_2fa <user>` on the host; superuser break-glass documented |
| Backup restore untested | 1 | Medium | Restore drill in CI (snapshot → restore → integrity check) |
| Monitor refactor breaks existing checks | 2 | Low | `kind=http` default, existing tests unchanged, migration test |
| Channel migration loses secrets | 2 | Low | Data migration with a test on encrypted fixtures; old columns dropped one release later |
| Status page leaks internal details | 2 | Medium | Allow-listed fields only; a test asserts no URL or IP appears in the output |
| Rolling-job state machine edge cases | 3 | Medium | Explicit state table, property-style tests, resume/abort audited |
| Remote upgrade as a privilege-escalation path | 3 | Medium | Opt-in; tag-only refs; fixed repository; checksums; security review before release |
| `ping` needs `NET_RAW` in Docker | 2 | High | Documented `cap_add`; TCP check suggested as the alternative |
| Scope creep toward Zabbix/Datadog | all | High | Phase gates; the "deliberately not planned" list in `plan.md` |

---

*Next step: start Phase 0.1 (CI), then 1.1 (watchdog). Each item above is sized to be one
pull request.*
