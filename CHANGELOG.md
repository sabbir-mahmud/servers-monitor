# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Releases marked **agents must be upgraded**
change files under `agent/`: run the upgrade command from each server's page.

## [2.2.0] - unreleased · agents must be upgraded

### Added
- **System page** (Administration → System): relay, watchdog, backups, Redis usage, agent
  versions and configuration at a glance.
- **Watchdog**: the relay pings `WATCHDOG_URL` every minute; after downtime, every channel is
  told how long monitoring was blind.
- **Backups**: daily database snapshots with rotation, `manage.py backup` / `restore`, and an
  optional Litestream service for continuous off-site copies.
- **Two-factor sign-in** with an authenticator app and recovery codes; required for Admins by
  default (`REQUIRE_2FA_FOR_ADMINS`). Break-glass: `manage.py reset_2fa <user>`.
- **Redis usage meter** with a month-end projection and estimated cost.
- **Disk-full forecast**: "fills in ~6 days" on server pages and an alert within 7 days.
- **Monitor types**: TCP port (with TLS certificate expiry), ping, DNS record, domain
  registration expiry (RDAP), and **Heartbeat** monitors for cron jobs and backups
  (`smctl run NAME -- CMD` / `smctl ping NAME`) · **agents must be upgraded** for heartbeats.
- HTTP checks: POST/PUT with a body, encrypted request headers, "down if keyword appears",
  redirect control, and a **Slow** state for responses over a time limit.
- **Notification channels**: email, ntfy, Gotify, Pushover (emergency priority for critical),
  PagerDuty (incidents open and resolve with alerts), Slack and Discord with rich formatting.
- **Status pages**: public pages with daily uptime and incident updates, published as static
  files to a GitHub repository (GitHub Pages or Cloudflare Pages), or downloaded.
- **Containers, top processes, temperatures and GPUs** on server pages (read from cgroup
  files: no Docker access needed), with alerts when a container stops or a sensor runs hot ·
  **agents must be upgraded**.
- **Rolling jobs**: run an action in batches with a pause, health-gate monitors and
  stop-on-failure; Resume or Abort a halted rollout.
- **Schedules**: run an action on a cron expression across servers, groups and environments.
- **Uptime checks from your servers**: HTTP and TCP monitors can also run on chosen agents
  (`smctl probes on`), for internal URLs and multi-location checks with a quorum. No extra
  Redis commands · **agents must be upgraded**.
- **REST API** (`/api/v1/`) with personal tokens (read-only or run actions, expiring,
  revocable) and **Prometheus metrics** at `/metrics`. See `docs/api.md`.
- **Long-term history**: hourly rollups kept 400 days; charts for 30 and 90 days; status pages
  show 90 days; disk forecasts per mount.
- **Live log follow**: stream new lines of a log or a systemd unit's journal for up to 10
  minutes, with a filter · **agents must be upgraded**.
- **Installable web app** with an offline page, and **browser push** notifications
  (end-to-end encrypted, no third-party library).
- **Agent upgrades from the dashboard**, no SSH: opt in with `smctl remote-upgrade on`, then
  *Upgrade agent now* per server or *Upgrade all outdated* in batches (release tags only,
  checksum-verified, root re-validates every request) · **agents must be upgraded** once by
  hand to get it.
- **Single sign-on (OpenID Connect)** with group-to-role mapping; local accounts kept for
  break-glass. New dependency: `mozilla-django-oidc`.
- CI (lint, tests, agent on Python 3.11–3.13, shellcheck, Docker build) and tagged releases
  with agent checksums and a container image.

### Security
- Outbound checks and notifications now refuse internal addresses **before** connecting, so
  error messages can't reveal which internal ports are open.

## [2.1.0] - 2026-10-11 · agents must be upgraded

### Security
- Relay messages are signed end to end with a per-server key Redis never sees; agents keep
  a replay list.
- Verified installs and upgrades: commands carry checksums of the deployed agent files.
- Superusers can only be changed by superusers; Django admin login uses the throttled login.
- Login throttling per account, per account + IP and per IP.
- HTTPS by default in production; `X-Forwarded-Proto` trusted only with `TRUST_PROXY_HEADERS`.
- No open redirects; changing your email needs your password and notifies the old address.
- SSRF guard for health checks and webhooks (`MONITOR_ALLOW_PRIVATE_TARGETS`).
- Telegram tokens, webhook URLs and enrolment credentials encrypted at rest.
- Validated systemd unit names in sudo rules; credentials read at hidden prompts.
- Per-server message rate limits, heartbeat throttling and a 256 KB message cap.

### Added
- User management: invitations, optional sign-up with approval, roles, deactivation.
- Password reset by email or Admin-generated link; "My account" page.
- Notification channels (Telegram, webhooks) with routing and reminders; maintenance windows.
- Application monitors (HTTP checks, uptime bars, TLS expiry); collapsible sidebar.

## [2.0.0] - 2026-10-11 · agents must be upgraded

### Changed
- Agents and the dashboard talk through a Redis relay (no public API, no inbound ports).
- SQLite replaces PostgreSQL; the relay process replaces Celery.
- Agents install from GitHub; `smctl` manages them on each server.
