# v3 plan: account types, device monitoring, multi-tenancy

*November 2026. Turns Server Monitor (single-team, Linux servers) into one product for
three kinds of account: **Parent**, **Company**, **Solo developer**.*

---

## 1. Where the project is today

| Area | Today | What it means for v3 |
|---|---|---|
| **Tenancy** | One install = one team. Every `name` is globally `unique=True` (servers, groups, monitors, channels, schedules, status pages). Roles (Viewer/Operator/Admin) are global Django Groups. | No notion of "my account vs yours". This is the **biggest change**: everything must be scoped to an account. |
| **Users** | Invite-only, optional self sign-up with Admin approval, 2FA, OIDC SSO. | Sign-up must create an *account* and pick a type. Invitations become per-account. |
| **Monitored things** | `fleet.Server`: Linux + systemd, Python 3.11 agent, `psutil`. | Has to become a general **Device** (server, desktop, laptop, phone, tablet) on 5 OSes. |
| **Transport** | Redis relay, one ACL user + 2 keys per server, agent BLPOP long-poll, signed messages. No inbound ports anywhere. | Fine for servers. **Wrong for phones**: they can't hold a long poll (battery, OS kills it), and iOS can't run a background daemon at all. Phones need HTTPS check-ins + push (FCM/APNs). |
| **Database** | SQLite (WAL), one relay process. | Fine for one team. A hosted, multi-account service needs **PostgreSQL**. |
| **App monitors, commands, alerts, status pages, API, audit** | Mature (≈380 tests). | Keep almost all of it; scope it by account and gate it by account type. |

**Reuse score:** roughly 70 % of the code carries over (monitors, checker, alerts, channels,
operations, rollouts, schedules, status pages, signing, audit, 2FA). What's new is tenancy,
the device model, location, mobile/desktop agents, and per-type UI.

---

## 2. The product, per account type

| Capability | Parent (family) | Solo developer | Company |
|---|---|---|---|
| Monitor devices: macOS, Windows, Linux, Android, iOS | ✅ kids' devices | ✅ own devices | ✅ company devices, assigned to staff |
| Last online, battery, OS/version, storage, network | ✅ | ✅ | ✅ |
| Last location + history, places (home/school) with arrive/leave alerts | ✅ | ✅ (opt-in, e.g. lost laptop) | ✅ policy-controlled, off by default |
| "Locate now", "Ring device" | ✅ | ✅ | ✅ |
| App usage / screen time (where the OS allows) | ✅ | — | optional |
| Servers (Linux agent, services, containers, logs) | — | ✅ | ✅ |
| Web / API / TCP / DNS / heartbeat monitors, status pages | — | ✅ | ✅ |
| Custom commands (allowlisted actions, rollouts, schedules) | — | ✅ | ✅ |
| Members | Owner + co-parent(s) | just the owner | Owner, Admins, Operators, Viewers, **Staff** |
| People to monitor | **Children** (profiles, no login needed) | — | **Staff** (members, devices assigned to them) |

The UI follows the type: a parent never sees "Servers", "Rollouts" or "Status pages"; their
home screen is a card per child with each device's last seen time, battery and a map.

### Consent and store rules (design requirement, not an afterthought)

Google Play's stalkerware policy and Apple's review rules allow monitoring apps only when
they are **visible**: a persistent notification on Android, a clear in-app disclosure, an
icon that isn't hidden, and enrolment done on the device itself. Employee monitoring
also brings notice-and-consent duties (GDPR and similar laws). So:

- Enrolment always happens **on the device**, by scanning a QR code / entering a code, with
  a screen that says who will see what. No silent install, no hidden mode.
- The device app always shows that it's monitored, and by which account.
- Company accounts get a **policy** per device group (what is collected: location on/off,
  work hours only, app usage on/off), shown to the staff member on their device.
- A device can be marked **company-owned** or **personal (BYOD)**; BYOD defaults to no
  location and no app usage.
- Location history has a retention setting (default 30 days) and is deleted with the device.

This keeps the app publishable on both stores, and it's what parents and companies need
to be told anyway.

---

## 3. Architecture changes

### 3.1 Tenancy: `Account` + `Membership`

```
Account(id, name, kind: family|solo|company, plan, created_at, settings JSON)
Membership(account, user, role: owner|admin|operator|viewer|guardian|staff, invited_by)
Person(account, name, kind: child|staff, user nullable, avatar)   # who a device belongs to
```

- Every owned model gets `account = FK(Account)`: `Server/Device`, `ServerGroup`, `Monitor`,
  `Channel`, `MaintenanceWindow`, `Job`, `Schedule`, `StatusPage`, `ApiToken`, `AuditLog`,
  `Alert`, `Invitation`.
- `unique=True` on names becomes `UniqueConstraint(fields=["account", "name"])`.
- Roles move from global Django Groups to `Membership.role`. Keep the existing permission
  matrix (`apps/users/roles.py`) but evaluate it per membership, e.g.
  `request.membership.can("operations.add_job")`.
- One `AccountMiddleware` sets `request.account` (from the URL `/<account-slug>/…` or the
  session's current account). One `AccountScopedMixin` / manager method
  (`Model.objects.for_account(request.account)`) replaces every bare `.objects.filter(...)`.
  13 modules query `Server.objects` today; all views, the API and the relay need this pass.
- A test that walks every URL as a user of account A and asserts nothing from account B is
  ever visible or writable. This is the security bar for the whole release.
- A user can belong to several accounts (a developer who is also a parent), with an account
  switcher in the sidebar.

**Migration of existing installs:** one data migration creates a `company` account, puts
every existing row into it and turns Django Group membership into `Membership` rows. Nobody
loses anything.

### 3.2 `Server` becomes `Device`

Rename the concept, keep the table (`db_table` unchanged for a cheap migration):

```
Device(account, person nullable, name, kind: server|desktop|laptop|phone|tablet,
       os: linux|macos|windows|android|ios, os_version, model, ownership: company|personal,
       transport: relay|https, last_seen_at, battery_percent, charging, storage_free, …)
DeviceLocation(device, recorded_at, lat, lon, accuracy_m, source: gps|wifi|ip)
Place(account, name, lat, lon, radius_m)            # home, school, office → geofence alerts
AppUsage(device, day, app_id, app_name, seconds)    # where the OS allows it
DevicePolicy(account, name, location, app_usage, work_hours, …)
```

The health model (`health_at`, online/late/offline) already fits; phones just get a longer
expected interval (15 min) and "offline" means "not checked in for N hours", configurable.

New alert kinds: `DEVICE_OFFLINE_LONG`, `BATTERY_LOW`, `PLACE_ARRIVED`, `PLACE_LEFT`,
`LOCATION_UNAVAILABLE` (permission revoked), `AGENT_REMOVED` (uninstall / tamper signal).
They reuse the existing dedup, hysteresis, routing and channels.

### 3.3 Two transports

| | Servers (today) | Desktops + phones (new) |
|---|---|---|
| Up | Redis RPUSH, signed | `POST /device/v1/checkin` over HTTPS, signed with the same HMAC scheme (`agent_api/signing.py`) |
| Down | BLPOP long-poll, < 1 s latency | The check-in response carries pending commands; urgent ones ("locate now", "ring") send a **silent push** (FCM / APNs) that triggers an immediate check-in |
| Redis cost | ~65 K commands/server/month | 0 Redis commands |

This means the dashboard must be reachable over HTTPS for devices. That's normal for a
hosted service. The "no inbound ports" promise still holds for **servers**, which keep the
relay.

### 3.4 Database and hosting

- PostgreSQL (Django 6 + psycopg 3), and keep SQLite for single-user self-hosting and tests.
- Background work: the relay process keeps doing relay work; move check scheduling, push
  sending and location/geofence processing to a task queue (Celery + Redis, or Django 6's
  `django.tasks` with a database backend to stay light).
- Location and app usage data go in their own tables with indexes on `(device, recorded_at)`
  and a retention prune like `MetricSample`.

---

## 4. Agents per platform (the hard part, be realistic)

| Platform | How | Last online | Location | App usage | Commands |
|---|---|---|---|---|---|
| **Linux server** | Existing Python agent (unchanged) | ✅ | — | — | ✅ allowlist, logs, rollouts |
| **Linux desktop** | Same agent, HTTPS transport, user-session helper | ✅ | ⚠️ GeoClue if present, else IP (city level) | ⚠️ active window (X11/Wayland varies) | ✅ allowlist (company/solo) |
| **macOS** | Python agent frozen with PyInstaller, signed + notarised `.pkg`, `launchd` daemon + a small Swift menu-bar helper for permissions | ✅ | ✅ CoreLocation (needs user permission, granted to the helper app) | ⚠️ frontmost app sampling; Screen Time API is iOS-only | ✅ allowlist |
| **Windows** | Same agent frozen, signed MSI, Windows Service (`pywin32`) | ✅ | ✅ Windows Location API (permission) | ⚠️ foreground window sampling | ✅ allowlist (PowerShell/argv) |
| **Android** | Native app (React Native bare or Kotlin): foreground service + persistent notification, WorkManager check-ins every 15 min, FCM | ✅ | ✅ Fused Location, background permission | ✅ `UsageStatsManager` (user grants Usage access) | locate, ring; no shell |
| **iOS / iPadOS** | Native app (Swift, or RN with native modules), APNs | ⚠️ "last check-in": significant-location-change + background refresh wake the app; iOS decides when | ✅ significant changes (~500 m), precise when "Locate now" push arrives | ⚠️ only via Apple's **FamilyControls / DeviceActivity** (needs Apple's Family Controls entitlement; reports are viewable on-device, not exported raw) | locate, ring (local notification + sound) |

Honest limits to tell users up front:

- **iOS** can never be a background daemon. "Last online" means "last time iOS let the app
  check in", usually within an hour while the phone moves or has signal. If the child
  force-quits the app, it stops until the next location change or launch. Detect that and
  alert ("Monitoring stopped on Sara's iPhone").
- Anything resembling parental *control* (blocking apps, screen-time limits) on iOS goes
  through the Family Controls entitlement and Apple's approval; Android needs Device Admin /
  Device Owner for strong tamper resistance. Treat control as a **later phase**; v3 is monitoring.
- Custom commands stay **allowlist-only** and only for company/solo accounts, on servers and
  desktops. Phones get a fixed set of safe actions (locate, ring, refresh).

Recommended stack for mobile: **React Native (bare)**, so one codebase covers Android and iOS,
with native modules for location, usage stats and push. Desktops: reuse the existing Python
agent so servers and desktops share collection code.

---

## 5. Phases

Each phase ships on its own and keeps all existing tests green.

### Phase 0: groundwork (1–2 weeks)
- PostgreSQL support + CI matrix (SQLite and Postgres).
- Task queue for background jobs.
- Decide hosting model (see §7) and publish a privacy notice draft.

### Phase 1: accounts and tenancy (3–4 weeks) — *blocks everything else*
- `Account`, `Membership`, `Person`; per-account roles; account switcher.
- Sign-up flow: choose **Parent / Solo developer / Company**, name the account, 2FA prompt.
- Add `account` FK to every owned model, per-account unique names, data migration for
  existing installs.
- `for_account()` scoping in every view, form, API endpoint, relay handler, export and
  management command.
- Cross-account isolation test suite (every URL, every API endpoint, relay messages,
  status-page publishing).
- Feature gating by account type (sidebar, URLs return 404 for features the type doesn't have).
- Company: invite **staff** and team members; Parent: invite a co-parent; Solo: no members.

### Phase 2: devices on desktops (3–4 weeks)
- `Server` → `Device` (kind, os, person, ownership, battery…), `DeviceLocation`, `Place`.
- HTTPS check-in endpoint, signed; enrolment by QR code / 8-character code that expires.
- Agent: remove the systemd/`/proc` assumptions behind a platform layer; macOS (`launchd`)
  and Windows (service) packaging; IP-based coarse location; OS location where permitted.
- Device page: last online, battery, storage, OS, map with last location and history.
- Parent home: one card per child, their devices, last seen, a map.

### Phase 3: Android app (4–6 weeks)
- Enrol by QR, persistent "Monitored by <account>" notification, check-ins, location,
  battery, FCM for locate/ring, usage stats (opt-in by policy).
- Tamper signals: permission revoked, app disabled, notification dismissed, uninstall
  (device stops checking in → alert).
- Play Store listing in the "Parental control" / enterprise category with the required
  disclosures.

### Phase 4: iOS app (4–6 weeks)
- Same app, APNs, significant-location monitoring, background refresh, locate/ring.
- Apply for the Family Controls entitlement early (it can take weeks); add DeviceActivity
  reports once granted.

### Phase 5: parent & company features (3–4 weeks)
- Places + arrive/leave alerts, battery-low, offline-for-N-hours, daily digest email.
- Company policies (what's collected, work hours only), BYOD vs company-owned, per-staff
  device list, staff can see what's collected about their own device.
- Location/app-usage retention settings, export and delete per person (GDPR).

### Phase 6: commercial (optional, 2–3 weeks)
- Plans and limits per account type (devices, monitors, retention), Stripe billing,
  usage page, trial.

Total: roughly **5–7 months** for one developer; phases 3 and 4 can run in parallel with a
second (mobile) developer.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| A tenancy bug leaks one family's locations to another account | Scoping only through `for_account()`, a lint check banning bare `.objects` in views, and the cross-account test suite as a release gate |
| Store rejection (stalkerware policy) | Visible app, on-device enrolment, clear disclosure, parent/enterprise positioning only (§2) |
| iOS background limits make "last online" look broken | Set expectations in the UI ("iPhones check in when they move or about hourly") and alert when monitoring stops |
| Location data is sensitive (breach impact) | Encrypt coordinates at rest (`EncryptedTextField` exists), short default retention, audit every location view |
| Redis cost grows with devices | Devices use HTTPS, not the relay |
| Scope (5 OSes + tenancy) | Ship phases 1–2 first: desktop + server monitoring for all three account types is already a usable product |

---

## 7. Decisions (November 2026)

1. **Hosting:** managed by the owner; out of scope for this plan.
2. **Mobile stack:** React Native with **Expo**, built and released with **EAS**.
3. **Scope:** decided as we go, phase by phase. Monitoring first; no parental controls yet.
4. **Company staff:** both. Staff are `Person`s; a staff member can also have a login
   (`Person.user`) and a role.
5. **Naming:** the product is **Vigil** (`SITE_NAME`). The agent, `smctl`, its systemd units
   and the `sm:` relay keys keep their names so existing servers need no change.

## 8. Progress

- **Phase 1 — done** on branch `feat/v3-accounts` (not yet merged). `apps/tenants`: Account
  (family / solo / company), Membership (owner, admin, guardian, operator, viewer), Person;
  permissions come from the membership in the current account (no Django groups); every
  owned model has an `account` with per-account unique names; expand → backfill →
  contract migrations put existing data in a "My company" account. Feature gating per
  kind, account switcher, sign-up with kind, people pages, invitations per account (an
  existing login joins instead of signing up again). `tests/test_isolation.py` checks every
  object URL, list, form, the API, channel routing, maintenance and schedules across two
  accounts. Differences from the plan: the System page is superuser-only, and sign-up no
  longer waits for approval.
- **Next: Phase 2** — `Device` on desktops (macOS, Windows, Linux desktop agents, HTTPS
  check-ins), attached to a `Person`.
