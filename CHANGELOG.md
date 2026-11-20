# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Releases marked **agents must be upgraded**
change files under `agent/`: run the upgrade command from each server's page.

## [3.0.0] - unreleased

The project is now **NexGuard**: one install serves many separate accounts. Agents keep
working without an upgrade (`server-monitor-agent`, `smctl` and the relay keys keep their
names); upgrading gets the relay backlog fixes below.

### Added

- **Server tasks** (**agents must be upgraded** to 2.3.0): check for updates, upgrade
  packages, clean up and reboot from a server's page, from Run on servers, or on a
  schedule. Works with apt, dnf, yum, zypper, apk and pacman. Each server opts in per task
  (`sudo smctl tasks on`), which writes one exact sudo rule each; checking for updates is
  open to Operators, the rest to Admins. Servers needing a reboot after updates say so.
- **Scripts** (Scripts in the sidebar): write a Bash, sh or Python script once and run it
  on chosen servers as a job, with output per server. Owner and Admin only; runs as the
  agent user on servers with shell turned on; audited (`script.save`, `script.delete`).
- **Saved SSH logins**: tick "Save this login" when connecting, and later connect in one
  click from the Terminal page or the SSH menu on a server's page. The password or key is
  stored encrypted (from `SECRET_KEY`), only after it worked, and is private to the person
  who saved it; it's decrypted only to open the connection and never sent back to a
  browser. Audited (`ssh.login_saved`, `ssh.login_removed`).
- **SSH terminal in the browser** (Terminal in the sidebar, or SSH on a server's page):
  user, IP, port and a password or private key open an interactive terminal (xterm.js).
  Owner and Admin only; host keys trusted on first use per account; audited (`ssh.open`, `ssh.close`, `ssh.failed`); idle and abandoned sessions
  close. New dependency: `paramiko`. New settings: `SSH_ALLOW_PRIVATE_TARGETS`,
  `SSH_IDLE_MINUTES`, `SSH_MAX_SESSIONS`.
- **Device alerts**: a computer or phone not reporting for `DEVICE_OFFLINE_ALERT_HOURS`
  (4), and battery low and not charging (15 %, critical at 5 %). Channels get a "Device
  alerts" switch, on by default.
- **Places**: name Home or School on a map (coordinates, a map link, or where a device is
  now) and hear "Sara arrived at School" and "Sara left Home", plus "hasn't arrived by
  08:30" on chosen days. Announced once; coarse fixes and edge wobble are ignored.
- **SOS** in the phone app: press and hold to send a critical alert with a map link to
  where the phone is, through the relay; "I'm OK" closes it.
- **Public status pages**, served by the dashboard at `/status/<name>/`: current state, 90
  days of daily uptime, incidents and maintenance for chosen applications. No sign-in for
  visitors, nothing to publish, no URLs or error text shown.
- **Weekly report** to channels that ask for it (on by default for new ones), Sundays at
  18:00: screen time, top apps and sites and arrivals per person, quiet devices,
  application uptime and server alerts.
- **Tamper alerts**: the device agent tells the dashboard when it's uninstalled and when
  someone edited its blocked-sites list (it puts it back); dropping from the protected
  service to user mode is reported; a phone reports unpairing on itself and location being
  turned off. **Rebuild the device agent and the phone app** to get these.
- **Accounts** of three kinds. **Parent**: children and the people who look after them.
  **Solo developer**: servers, uptime monitors, jobs and the API, just you.
  **Company**: all of that plus a team. Every server, monitor, alert, channel, job, schedule,
  API token, invitation and audit entry belongs to one account, and accounts
  never see each other's data (`tests/test_isolation.py`).
- One login can belong to several accounts with a different role in each (owner, admin,
  guardian, operator, viewer), with an account switcher in the sidebar. Inviting an
  address that already has a login asks them to sign in and join.
- **People**: the children of a family, or company staff, with or without a login. Devices
  attach to them in the next release.
- **Devices** for parent and company accounts: add a child's Windows, Mac or Linux
  computer, Android phone or iPhone/iPad and get a pairing code (and a QR code for the
  phone app). The code works until it's used; there's no time limit. Pairing gives the device
  a token that never expires: it keeps reporting until the parent removes it or pairs it
  again (after a reset or for a replacement), which cuts off the old one. Each device
  shows when it was last online, its battery, storage, network and last location, with a
  location history kept for `DEVICE_LOCATION_DAYS` (30). Devices check in over HTTPS at
  `/device/v1/pair` and `/device/v1/checkin`; `agent/nexguard_device.py` is a reference
  client for computers.
- **Connect a computer with one link**, for people who aren't technical: the pair page
  for a Windows, Mac or Linux device gives a link (`/d/K7PQ-3MXA`) to open on that
  computer. It shows three steps for that system: open PowerShell or Terminal, paste one
  line, type `y`. Nothing to install first, no Python, no administrator rights. The check-in
  runs as the user every 5 minutes (Task Scheduler, a LaunchAgent, or a systemd user
  timer/cron), and uninstalls itself when the device is deleted on the dashboard. See
  [docs/devices.md](docs/devices.md).
- **Device health**: computers report CPU, memory and GPU use with every check-in, and
  the device page has a Health card with current use and charts of CPU, memory, GPU,
  battery and storage (6 h to 30 days, kept for `DEVICE_METRICS_DAYS`, 30). The consent
  message names this data. Installed check-ins need the new script (1.1.0), which comes with
  pairing again.
- The device page's "right now" bar gains a second row with the latest CPU, memory and GPU
  readings, so they show from the first check-in instead of once a chart has two points.
- The computer check-ins (scripts 1.2.0 and the Go device agent) report the network: Wi-Fi,
  wired, mobile or none, and the Wi-Fi name where the system allows it (macOS 14+ and
  Windows 11 24H2+ hide it from apps without location access, so those usually show just
  "Wi-Fi"). The consent message and the connect page say so. Pair again to get the new
  script.
- **Device agent** (`device-agent/`, Go, version 0.1.0): one static binary for Windows,
  macOS and Linux (x86-64 and ARM). It runs as a system service that only an administrator
  can stop or remove. `nexguard pair`, `install`, `status`, `checkin` and `uninstall`.
  It reports the same health readings as the scripts. `make device-agent` tests it and
  builds it, and CI tests it on Linux, macOS and Windows.
- **The one-line connect command installs the device agent** as a protected service,
  asking for the computer's administrator password (sudo on Mac and Linux, a Windows
  permission prompt), so blocking works. Without an administrator it installs the same
  agent running as the person (login item, systemd user service, or sign-in task), when the
  dashboard has agent builds (`DEVICE_AGENT_DIR`, served at `/d/agent/<file>` and checked
  against `SHA256SUMS`); otherwise the health-only script as before. That agent does
  health, screen time and websites; blocking still needs the administrator install. The
  agent takes `--user` and `--dir`, and reports `agent_mode`.
- Browsers the system won't let the agent read (macOS without Full Disk Access) are named
  on the device page with the steps to allow it, instead of being skipped silently, and
  the connect command asks for Full Disk Access on a Mac (opens the settings page, waits
  until it's on). Arc history on a Mac.
- **Computer location**, a Location switch under Controls: every few minutes from the
  agent, Wi-Fi accurate through the Windows location service or GeoClue on Linux, and
  town-level from the internet address otherwise (every Mac, or location services off),
  using the free DB-IP city database the relay downloads (`DEVICE_GEOIP_URL`).
- **Screen time, websites and blocking** on a child's or staff member's computer (or your
  own, in a solo account) running the device agent, switched on per device under **Controls**. Screen time by app (Mac, Windows;
  total only on Linux), websites as site + visits + time from Chrome, Edge, Brave, Vivaldi,
  Opera, Firefox and Safari (pages and searches never leave the computer), and hosts-file
  blocking: adult sites (`ADULT_BLOCKLIST_URL`, refreshed daily), SafeSearch, and the
  parent's own block/allow lists. Never available for partners, relatives or shared
  devices. Pairing names it. Kept `DEVICE_ACTIVITY_DAYS` (30). New
  `GET /device/v1/blocklist`; check-ins carry `activity` and get `controls` back.
- `POST /device/v1/pair` with `"preview": true` shows who will see the device without
  using the code, and `Accept: text/plain` answers as `key=value` lines for shell clients.
- Sign-up (`ALLOW_SIGNUP`) asks for the account kind and makes the person its owner.
- `OIDC_ACCOUNT`: which company account SSO users join.
- **Phones check in through the Redis relay** (Upstash REST), not to the dashboard, so they
  report from anywhere while the dashboard stays private. Pairing a phone creates its own
  ACL user `sm-d-<id>` (only `RPUSH`/`PING` on `sm:d:<id>:in`) and returns a REST token and a
  signing key. The relay process applies signed `ci` messages and drops forged, stale or
  too-frequent ones. Removing a phone deletes its user. New `RELAY_REST_URL`, and
  `Device.relay_salt` (migration `devices.0008`). Phones need an Upstash `REDIS_URL`.
- **NexGuard phone app** for Android and iOS (`phone-app/`, Expo SDK 58, runs in Expo Go):
  scan the pair page's QR code or type the code, read what the account will see, and pair
  on *Allow*. It reports battery, storage, network and (if allowed) location while open
  and each time it's opened, and from the background in a development or store build.
  Built with EAS (`phone-app/eas.json`). `make phone-app-apk` builds the Android `.apk`,
  which the dashboard serves at `/d/app/NexGuard.apk` (`PHONE_APP_DIR`). The Android pair
  page shows a QR code for the phone's camera to download it. iPhones get `IOS_APP_URL`
  (TestFlight or the App Store). The release builds also connect to plain `http://`
  dashboards (home networks). `make phone-app` runs it in Expo Go for development. The
  phone pair page warns when the dashboard's address is `127.0.0.1`, which a phone can't
  reach.
- Device agent 0.2.0 keeps its own log in `logs/nexguard.log` next to its config: one JSON
  line per event (started, paired, checked in, settings switched on or off by the dashboard,
  errors), readable by everyone, rotated at 1 MB. No token, website or app names.
  `nexguard status` prints where it is. New: `nexguard --user --dir <folder> install`, which sets the agent to run at sign-in as that
  person (the same LaunchAgent, systemd user service or sign-in task the connect scripts use).

### Changed
- Roles are per account memberships, not Django groups. `manage.py sync_roles` is gone.
  Migrating puts existing data in a company account named "My company" (rename it under
  *Account settings*), the first superuser as owner, and keeps everyone's role.
- Server, monitor, channel, schedule and status page names are unique per account.
- The **System page** is for superusers only: it covers the whole installation.
- 2FA is required for owners and admins of solo and company accounts (and superusers),
  not for parents.
- Admins can only manage the login (email, password reset, 2FA, deactivation) of people
  whose only account is theirs; superusers can manage any login.
- After sign-in you land on Home: servers if the account has them, otherwise people.
  The server dashboard moved to `/servers/`.

### Changed
- The server pages moved from `/servers/…` to `/devices/` (the list) and `/devices/servers/…`
  (each server). Old links, including those in alert emails already sent, redirect
  permanently. The API keeps `/api/v1/servers`.
- Servers and computers/phones now share one table, `devices_machine` (name, account,
  hostname, last seen). Server and device details stay in `fleet_server` and
  `devices_device`, linked by the same id, so existing links, commands, alerts and history
  are unchanged. Names are unique per account across both, ignoring case; the migration
  renames a server to "name (2)" if it clashes with a device.
- Solo developer accounts call them **Devices** and can add a Linux server or connect their
  own computer or phone from the same page.

### Fixed
- Declining on the device no longer uses up the pairing code: the computer client asks
  first (a preview) and pairs only after a yes. Before, the code was spent even on a no,
  despite the message saying it still worked.
- Re-running the agent installer on a server that already had a config (for example one
  deleted and added again on the dashboard) kept the old, dead relay login without asking
  and still printed "Done." It now asks for the new connection string (Enter keeps the
  current one), checks it with the relay and switches to it, keeping services, logs and
  actions. `smctl upgrade` doesn't ask. A failed test heartbeat now stops the installer
  with an error instead of reporting success.
- The consent message on computers no longer claims they share location or network; it
  names what they actually send (battery, storage and system version).
- After the relay process was down, the heartbeats queued in Redis were taken as fresh
  when it came back: a server that died meanwhile showed online with old numbers for a
  few minutes. Agents now stamp each heartbeat with its (signed) send time and the
  dashboard skips any older than the "late" window plus a minute of clock drift. Agents
  that don't send it are handled as before; upgrade them to get the fix.
- The agent keeps at most 500 messages queued in Redis while the dashboard is down
  (was 5000, about 32 MB per server with heartbeats around 6 KB).

### Removed
- **Docker**: `docker-compose.yml`, `main/Dockerfile`, `make docker-up`, the CI image build
  and the GHCR image on releases. Run the dashboard with `./run.sh` (web + relay in one
  terminal) or the systemd units in `deploy/`. Data in an old Docker `data` volume: copy
  `/data/db.sqlite3` out and point `SQLITE_PATH` at it.
- **Status pages** (public pages published to GitHub Pages or Cloudflare Pages). Migration
  `core.0003_drop_status_pages` drops their tables and permissions: export anything you
  need from them before upgrading. Pages already published to GitHub stay up until you
  delete that repository; they just stop updating.
- Sign-up waiting for administrator approval: a new sign-up gets its own empty account.

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
