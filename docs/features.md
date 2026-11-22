# Features

Everything NexGuard does, grouped by area. Each section links to the doc with the
details.

- [Accounts and people](#accounts-and-people)
- [Servers](#servers)
- [Operations and deploys](#operations-and-deploys)
- [Application monitors](#application-monitors)
- [Alerts and notifications](#alerts-and-notifications)
- [Computers (Windows, Mac, Linux)](#computers-windows-mac-linux)
- [Phones (Android, iPhone)](#phones-android-iphone)
- [How each kind of machine connects](#how-each-kind-of-machine-connects)
- [API and integrations](#api-and-integrations)
- [Running the dashboard](#running-the-dashboard)
- [Security](#security)
- [What each account kind gets](#what-each-account-kind-gets)
- [Suggested features](#suggested-features)

## Accounts and people

- **One install, many accounts**, each fully separate: no account ever sees another's
  servers, devices, alerts, jobs, tokens or audit log.
- **Three account kinds:**
  - **Parent:** children's phones, tablets and computers.
  - **Solo developer:** your own servers, websites, devices and API, just you.
  - **Company:** everything a solo developer gets, plus a team and staff devices.
- **Roles per account:** owner, admin, guardian (a second parent), operator and viewer.
  One login can belong to several accounts with a different role in each, switched from
  the sidebar.
- **People:** the children of a family or a company's staff, with or without a login.
  Devices attach to a person.
- **Joining:** by invitation. Inviting an address that already has a login asks that
  person to sign in and join. Optional open sign-up (`ALLOW_SIGNUP`) asks which account
  kind to create.
- **Sign-in:**
  - two-factor with an authenticator app and recovery codes. It's required for owners
    and admins of solo and company accounts, and for superusers;
  - single sign-on (OIDC) with group-to-role mapping (`OIDC_ACCOUNT` picks the company
    account);
  - self-service or admin password resets.
- **Account settings:** rename the account, see its owners, and switch or leave
  accounts.

See [security.md](security.md#accounts-and-the-web-ui) and
[configuration.md](configuration.md#accounts).

## Servers

- **Fleet dashboard:** CPU, memory, disk, load, network and uptime for every server,
  refreshed live. Filter by environment, group or health.
- **Disk forecasts:** "disk fills in ~6 days", per mount.
- **Server page:** ring gauges; services with state and version (systemd, Docker, pm2,
  process, HTTP); containers; top processes; temperatures and GPUs; disks; charts from 1
  hour to 90 days.
- **Long-term history:** rolled up, so charts stay fast after raw samples are pruned.
- **Agent:** one Python file plus `psutil`, run by systemd as an unprivileged user.
  - **Verified install:** one command, every file checked against checksums from your
    dashboard.
  - **Local allowlist** (`agent.toml`): the server decides which services, logs and
    actions the dashboard may use.
- **`smctl` on each server:** detect services, add deploy actions, manage sudo rules, wrap
  cron jobs, turn on probes and remote upgrades, diagnose problems, upgrade.
- **Agent upgrades from the dashboard:** one server or the whole fleet in batches, with no
  SSH. Opt-in per server, release tags only, checksum-verified.

See [agent.md](agent.md).

## Operations and deploys

- **Run allowlisted actions** on one server or many, and watch the output stream in.
- **Rolling deploys:** batches with health gates between them.
- **Scheduled jobs** on a cron schedule.
- **Service control:** restart services; tail or **follow logs live** (files and systemd
  journals).
- **Optional shell:** audited.
- **SSH terminal in the browser** (Owner and Admin): type the user, IP and port, sign in
  with a password or private key, and get a full interactive terminal (`top`, `vim`,
  `sudo`). The dashboard is the SSH client, so the server's SSH port must be reachable from
  the dashboard. Passwords and keys are used once and never saved; host keys are saved on
  first connection and must match afterwards; opening and closing are audited; idle
  sessions close.
- **Safe delivery:**
  - jobs run at most once and never re-send;
  - a job expires if it can't be delivered within 5 minutes, so an offline server never
    runs a stale deploy later;
  - an outbox retries when Redis is briefly unreachable.

See [agent.md](agent.md#scheduled-and-rolling-jobs) and
[architecture.md](architecture.md#job-lifecycle).

## Application monitors

- **Check types:** HTTP(S) (GET, HEAD, POST, headers, body keywords, slow thresholds),
  TCP port, ping, DNS record and domain expiry.
- **Heartbeat monitors** for cron jobs and backups (`smctl run` / `smctl ping`).
- **Checks from many places:** from the dashboard and from your own servers (probes),
  with a quorum before something counts as down.
- **Results:** uptime bars, response-time charts, incidents, certificate expiry.
- **Public status pages** at `/status/<name>/`, served by the dashboard (no sign-in for
  visitors): chosen applications' state, 90 days of daily uptime, the last 14 days of
  incidents and planned maintenance. Names and up/down only, never URLs or errors. Can be
  kept as a draft only the account sees.

See [architecture.md](architecture.md#application-health-checks).

## Alerts and notifications

- **Alerts:** server offline, high CPU, high memory, disk filling up, disk will fill up
  (forecast), service down, container stopped, running hot, application down, certificate
  expiring, domain expiring.
- **Device alerts:** not reporting (after `DEVICE_OFFLINE_ALERT_HOURS`, 4 by default),
  battery low and not charging (15 %, critical at 5 %), places, SOS and tampering. A
  channel chooses whether it gets them.
- **No flapping:** alerts are deduplicated, with hysteresis.
- **Channels:** Telegram, email, Slack, Discord, Microsoft Teams (webhook), ntfy, Gotify,
  Pushover, PagerDuty, webhooks and **browser push**.
- **Delivery:** routing rules, reminders until acknowledged, retries and a delivery log.
- **Maintenance windows:** schedule one, or press **Mute** on a server or app. Alerts are
  still recorded, but nobody gets paged.
- **Weekly report** on Sunday at 18:00 to each channel that wants it: screen time, top
  apps and sites and arrivals per person, devices that went quiet, application uptime and
  server alerts, whichever the account has.

See [architecture.md](architecture.md#alerts-and-notifications).

## Computers (Windows, Mac, Linux)

- **Connect with one link:**
  - the pair page gives a link (`/d/K7PQ-3MXA`) to open on the computer;
  - the link shows one line to paste into PowerShell or Terminal for that system;
  - it shows who will see the computer and asks first; saying no keeps the code valid;
  - nothing to install first.
- **NexGuard device agent** (Go, one static file, x86-64 and ARM):
  - with the administrator password, a **protected service** only an administrator can
    stop, which can block websites;
  - without it, the same agent **running as that person**, with no blocking.
- **Health:** CPU, memory, GPU, battery, storage, network (Wi-Fi, wired or mobile, with
  the Wi-Fi name where the system allows) and system version. Charts from 6 hours to
  30 days.
- **Location** (a switch under Controls):
  - Wi-Fi accuracy from the Windows location service or GeoClue on Linux;
  - none on a Mac, which doesn't share a position with background apps.
- **Controls**, only for a child's or staff member's computer, or your own in a solo
  account:
  - **Screen time:** minutes per day by app.
  - **Websites visited:** site, visits and time from Chrome, Edge, Brave, Arc, Vivaldi,
    Opera, Chromium, Firefox and Safari. Pages and searches never leave the computer.
  - **Blocking:** about 77,000 adult sites, SafeSearch on Google, Bing, DuckDuckGo and
    YouTube, and your own block and allow lists.
- **Tamper alerts:** uninstalling the agent tells the dashboard first; an edited blocked-sites
  list is put back and reported; dropping from the protected service to running as the
  person is reported.
- **Mac Full Disk Access:** the connect command asks for it; the device page names the
  browsers it can't read yet.
- **On the computer itself:**
  - `nexguard status` shows the pairing, the last check-in and the log;
  - the log is `logs/nexguard.log`, one JSON line per event, with no token, website or
    app names;
  - one line uninstalls it.
- **History:** activity, location and health are each kept 30 days
  (`DEVICE_ACTIVITY_DAYS`, `DEVICE_LOCATION_DAYS`, `DEVICE_METRICS_DAYS`) and deleted with
  the device.

See [devices.md](devices.md).

## Phones (Android, iPhone)

- **NexGuard phone app** (`phone-app/`, Expo SDK 58, built with EAS).
- **Getting the app:**
  - **Android:** your dashboard serves the `.apk` itself at `/d/app/NexGuard.apk`, and
    the pair page shows a QR code for the phone's camera;
  - **iPhone:** the pair page links to TestFlight or the App Store (`IOS_APP_URL`).
- **Pairing:**
  - scan the pair page's QR code from the app, or type the address and code;
  - the app shows exactly what the account will see, and pairs only on **Allow**;
  - then it asks once for location; without it, everything else is still shared.
- **Reports:** battery and charging, free and total storage, network type, model, system
  version, and location if allowed.
- **SOS button:** press and hold; everyone on the account gets a critical alert with a map
  link to where the phone is, repeated until someone acknowledges it. **I'm OK** closes it.
- **Tamper alerts:** turning location off after sharing it, and unpairing on the phone
  itself, are both reported.
- **When it reports:**
  - every 15 minutes while open, and each time it's opened;
  - in the background (at most every 15 minutes, when the system allows) in the EAS
    builds;
  - in Expo Go, only while open.
- **Reports from anywhere:** check-ins go through the Redis relay (Upstash), not to the
  dashboard, so a phone keeps reporting on mobile data or any Wi-Fi.
- **Removing a phone** on the dashboard cuts it off; the app then returns to its pairing
  screen. Unpairing in the app stops it on the phone.
- **Development:** the app runs in Expo Go (`make phone-app`).

See [devices.md](devices.md#add-a-phone-android-or-iphone) and
[phone-app/README.md](../phone-app/README.md).

## Places

- **Named places** (Home, School, the office): a point and a radius, set by typing
  coordinates, pasting a Google Maps or OpenStreetMap link, or "where this device is now".
- **Arrivals and departures:** "Sara arrived at School", "Sara left Home", sent once to
  every channel that takes device alerts. For everyone, or only chosen people.
- **Missed arrivals:** "Sara hasn't arrived at School (expected by 08:30)" on the days you
  choose; it closes when she arrives.
- **No false alarms:** coarse fixes (town-level) are ignored, a margin stops a fix on the
  edge from flapping, and drawing a place around someone doesn't announce an arrival.
- The **Places** page shows each place on a map and who's there now.

## How each kind of machine connects

| Machine | Talks to | How |
|---|---|---|
| Servers | Redis relay | Two lists per server (`sm:a:<id>:cmd` / `:in`), its own Redis user, signed messages |
| Phones | Redis relay (Upstash REST) | Its own Redis user: pushes signed check-ins to `sm:d:<id>:in`, reads signed settings (`:cfg`) and beeps (`:cmd`, Android); pairs through the relay |
| Computers | Redis relay | Its own Redis user: pushes signed check-ins to `sm:d:<id>:in`, reads signed settings (`:cfg`) and blocklist (`:bl`); pairs through the relay |

No machine needs a route to the dashboard: both sides dial out to Redis, and the dashboard
has no device API. Computers only fetch the install script and agent from it. See
[architecture.md](architecture.md#the-relay-protocol).

## API and integrations

- **REST API** (`/api/v1/`), with token authentication and per-account tokens: servers,
  monitors, alerts, and running actions.
- **Prometheus `/metrics`.**
- **Installable web app** with an offline page and browser push.

See [api.md](api.md).

## Running the dashboard

- **Small to run:** Django and SQLite, plus one background process (`manage.py relay`)
  that replaces Celery.
- **Watches itself:**
  - **watchdog:** pings `WATCHDOG_URL` every minute, so you hear if monitoring stops;
  - **downtime check:** notices when monitoring was down, and for how long;
  - **relay warning:** a banner when the relay process isn't running.
- **Backups:** daily database snapshots, plus optional Litestream.
- **System page** (superusers): relay health, a Redis usage meter with a cost estimate,
  and agent versions.
- **Runs anywhere:** a laptop, a Raspberry Pi or a VPS, with `run.sh` or systemd.

See [deployment.md](deployment.md).

## Security

- **No inbound ports** on servers. The dashboard can stay private as well; only computers
  need to reach it.
- **Signed messages:** commands and check-ins are signed (HMAC-SHA256) with per-machine
  keys that Redis never sees. Someone holding the Redis password can't run a command or
  forge a result.
- **Least privilege in Redis:** each server and each phone has its own Redis user,
  limited to its own keys and a few commands.
- **Verified installs:** checksums for the server agent, the device agent and installer
  scripts.
- **Web security:** encrypted secrets at rest, SSRF guards on outbound checks, and rate
  limits on sign-in and pairing.
- **Append-only audit log** for every account.
- **Consent first:** devices are told who will see what before anything is shared, and
  invasive controls are limited to children's, staff and your own computers.

See [security.md](security.md).

## What each account kind gets

| | Parent | Solo developer | Company |
|---|:-:|:-:|:-:|
| People | ✓ | | ✓ |
| Computers and phones | ✓ | ✓ (your own) | ✓ |
| Screen time, websites, blocking | ✓ (children) | ✓ (your own computer) | ✓ (staff) |
| Servers, operations, deploys | | ✓ | ✓ |
| Application monitors | | ✓ | ✓ |
| REST API | | ✓ | ✓ |
| Alerts and notifications | ✓ | ✓ | ✓ |
| Maintenance windows | | | ✓ |
| Team with roles | ✓ (guardians, viewers) | | ✓ |
| Audit log | ✓ | ✓ | ✓ |

## Suggested features

From comparing NexGuard with the tools people use for each of its jobs (November 2026).
These are ideas, not commitments; each names the competitor that has it.

### Competitors

| Area | Competitors | What they do that NexGuard doesn't (yet) |
|---|---|---|
| Uptime and server monitoring | Uptime Kuma, Beszel, Better Stack, Netdata | On-call rotations, log search |
| Parental control | Qustodio, Google Family Link, Bark, Apple Screen Time | Screen-time limits and schedules, web filtering by category, phone app usage |
| Family location | Life360, Find My, Family Link | "Locate now" and ring |
| Remote management (RMM) | Tactical RMM, NinjaOne, MeshCentral | Scripts on computers, software and patch inventory, remote desktop |

Where NexGuard is already ahead:
- **No inbound ports:** servers and phones reach it through Redis.
- **The server decides:** each server's own allowlist limits what the dashboard can do.
- **One self-hosted dashboard** for servers, uptime and family or staff devices, where
  others need two or three tools (Uptime Kuma *and* Beszel; Qustodio *and* Life360).
- **Consent before anything is shared.**

### Quick wins (done)

All six are built (November 2026) and described in the sections above.

1. **Device alerts.** Alerts for "phone hasn't reported for N hours", "battery low and not
   charging", and "computer offline". Today these show on the device page but never notify
   anyone. Every family-location app has them. It plugs into the existing alert rules and
   channels.
2. **Place alerts (geofences).** Name places (Home, School) on the map and get "Sara
   arrived at School" or "left Home", plus "didn't arrive by 8:30". Life360 and Family Link
   have them; Life360 also counts no-show alerts. It works from the check-in locations
   already stored.
3. **SOS button in the phone app.** One press pushes an `sos` message with the current
   location through the relay; the dashboard turns it into a critical alert on every
   channel. Life360's SOS is the model. It's one more message type on the phone's
   existing Redis list.
4. **Public status pages, again.** NexGuard had them (static pages published to GitHub or
   Cloudflare Pages) and removed them in 3.0. They're still a headline feature of Uptime
   Kuma and Better Stack. A simpler return: the dashboard serves a read-only page per
   account at its own URL (chosen monitors' uptime, incidents, maintenance), with nothing
   to publish. The uptime rollups are still kept.
5. **Weekly activity report.** An email or notification each Sunday: screen time per child,
   top sites, places visited, devices that went quiet, and server uptime. Qustodio's
   reports are its most-cited feature. It reuses stored data and the existing channels.
6. **Tamper alerts.** Alert when a computer's agent is uninstalled, its blocking was removed
   (the hosts file changed), or a phone was unpaired on the device. Qustodio and Bark both
   alert on tampering. The agent already notices when it puts blocking back.

### Medium

7. **Screen-time limits and schedules on computers.** A daily limit (2 h on school days)
   and quiet hours (no use after 21:00). The agent warns at 5 minutes left, then locks the
   screen or signs the person out. This is Qustodio's and Family Link's core feature.
   Screen time is already measured per minute; this adds enforcement in the agent.
8. **Web filtering by category.** Switches for gambling, social media, games, video and
   dating, next to the adult-site list. Qustodio filters dozens of categories. It needs
   more blocklist sources, and the hosts-file mechanism already exists.
9. **Computers through Redis.** Move the computer agent's check-ins to the relay (a second
   list per computer for settings coming back), so laptops report from anywhere like
   phones. The Go agent can speak Redis directly; the blocklist stays an HTTP download.
10. **On-call rotations and escalation.** Who gets paged this week, and who's next if
    nobody acknowledges within 15 minutes. Better Stack and PagerDuty do this. Today's
    "remind until acknowledged" is the first half.
11. **Anomaly alerts.** "CPU is twice its usual for this hour", from the stored history,
    like Netdata's anomaly detection. Disk forecasts already work this way.

### Larger

12. **Phone screen time and app usage.**
    - **Android:** usage stats, which need a native module (EAS build, not Expo Go) and a
      special access setting the person turns on.
    - **iPhone:** Apple's Screen Time API, which needs Apple's *Family Controls*
      entitlement.
    - Family Link and Apple Screen Time own this. It's the biggest gap for parents, and
      the most platform work.
13. **"Locate now" and "ring" for phones.** The dashboard asks a phone to report its
    position right away, or play a loud sound to find it. It needs a channel from the
    dashboard to the phone: push notifications (Expo Notifications, FCM and APNs). Family
    Link and Find My have both.
14. **Inventory and patches for staff computers** (company accounts). Installed apps,
    pending system updates, disk encryption and antivirus status, as NinjaOne and Tactical
    RMM show. The agent would read these locally and report them with check-ins.
15. **Allowlisted actions on computers** (company accounts). The same model as servers:
    the computer declares what's allowed, and the dashboard runs it (clear a print queue,
    restart a service, run an update). This is Tactical RMM's core feature.
16. **Log search for servers.** Keep chosen log files, search them, and alert on a
    pattern, as Better Stack does. Today logs can only be followed live.

### Deliberately left out

- **Reading messages, social media posts or photos** (Bark's approach). It's the most
  invasive kind of monitoring, needs access NexGuard shouldn't hold, and goes against
  telling the person exactly what's shared.
- **Keystroke logging and screenshots of staff** (some employee-monitoring tools). The
  same reason, and it's illegal in many places without strict conditions.
- **Built-in remote desktop.** Link out to MeshCentral or RustDesk instead: it's a large,
  security-sensitive product of its own.
