# Connecting computers and phones

Devices are the computers and phones of the people in an account: a child's laptop, a
staff member's work PC, your own Mac. They report when they were last online, battery,
storage and system version (phones also network and location). Servers are different:
they run the [agent](agent.md).

## Add a computer (Windows, Mac or Linux)

Nothing to install first, no administrator rights, no Python.

**On the dashboard** (the parent or admin):

1. **Devices → Add device** (or on a person's page), pick Windows, Mac or Linux, give it a
   name.
2. The next page shows a link like `https://monitor.example.com/d/K7PQ-3MXA`. Copy it
   and send it to the computer: email, chat, or type it into its browser.

**On the computer** (anyone can do this):

1. Open the link. It recognises the system and shows three steps.
2. Open **PowerShell** (Windows) or **Terminal** (Mac, Linux), as the page explains, and
   paste the one line it shows:

   | System | Line |
   |---|---|
   | Windows | `irm https://monitor.example.com/d/K7PQ3MXA/install.ps1 \| iex` |
   | Mac, Linux | `curl -fsSL https://monitor.example.com/d/K7PQ3MXA/install.sh \| sh` |

3. It says who will see the computer and asks **Allow this?** Type `y` and press Enter.

The dashboard's pair page turns to **Paired** within seconds. At the computer yourself?
The pair page has the same steps under *Do it from here*.

## Add a phone (Android or iPhone)

1. **Devices → Add device**, pick Android or iPhone, give it a name. The pair page shows a
   6-digit code (it works once, for 15 minutes) and a QR code to install the app.
2. Install the app:
   - **Android:** the dashboard serves it at `/d/app/NexGuard.apk`, the newest `.apk` in
     `PHONE_APP_DIR` (put your build there). Android asks once to allow installing from
     the browser.
   - **iPhone:** Apple doesn't allow installing from a download. The pair page links to
     `IOS_APP_URL`, NexGuard's TestFlight public link or App Store page.
3. Open NexGuard and type the code. It shows who will see the phone and what they can turn
   on (a preview: the code stays valid), then asks for each permission it needs, one at a
   time, saying why. Only then does it connect. Saying no to a permission still connects;
   the device page shows what's missing.

**Phones never talk to the dashboard.** Pairing, check-ins, SOS, settings and commands all
go through the Upstash Redis relay (HTTPS REST): the app pairs with a built-in login that
can only send pairing requests, and gets its own login, allowed only to push signed
check-ins to `sm:d:<id>:in` and read its signed settings (`:cfg`) and commands (`:cmd`).
So a phone reports from anywhere while the dashboard stays private. This needs an Upstash
`REDIS_URL` and the relay process (`manage.py relay`) running. See
[architecture.md](architecture.md#the-relay-protocol).

**When it reports.** When it opens, at the dashboard's interval while open, and from the
background when Android or iOS allows it (at most every 15 minutes). In **Expo Go** nothing
runs in the background, and there are no Android controls (below).

### Android controls (NexGuard 1.2)

For a child's or staff member's Android phone, or your own in a solo account, **Controls**
on the device page turns on the same things as on a computer:

| | How on the phone |
|---|---|
| **Screen time** | Minutes per app per day, from Android's usage statistics ("Usage access" for NexGuard). |
| **Websites visited** | The sites browsers look up (Chrome, Firefox, Edge, Samsung Internet, Brave and others), reduced to the site (`m.youtube.com` → `youtube.com`). Never pages or searches. Android 10 or newer. Time is approximate: a minute with a lookup counts as a minute there. |
| **Block adult websites** | DNS goes to Cloudflare's family resolver (1.1.1.3) instead of 1.1.1.1. |
| **SafeSearch, Also block / Always allow** | Answered on the phone; a site covers every name under it. |
| **Beep** | The device page's **Beep** rings the phone loudly for 30 seconds, even on silent, within about 30 seconds while it's online. |
| **Removal protection** | NexGuard is a *device admin*, so Android won't uninstall it. It can't be unpaired in the app either: only by deleting the device on the dashboard, which releases the protection so the app can be removed. |

Websites and blocking use a **website filter**: a VPN on the phone that routes only its own
DNS server address, so it sees name lookups and nothing else. A notification shows while
NexGuard runs. It checks the dashboard for a beep every 30 seconds (about 2,900 relay
requests a day per phone).

**Turned off on the phone** raises an alert at once: removal protection switched off
(Settings → Security → Device admin apps), the filter turned off or replaced by another VPN,
Private DNS set (which gets around the filter), usage access or notifications taken away.

Limits worth knowing:
- Removal protection can be turned off by whoever holds the phone; that's Android's rule
  for apps that aren't set up as a managed device. The dashboard is told when it happens.
- A browser's own secure DNS (set by hand in Chrome or Firefox) gets around the filter and
  can't be detected. Private windows still look names up, so they are recorded.
- iPhones get location, battery and SOS only: Apple allows screen time and blocking only
  through Family Controls, which needs Apple's approval.

## What gets installed

The NexGuard agent. Given an administrator password, it installs as a protected service
that only an administrator can stop, and that can block websites. Without one, it runs
**as the signed-in user** (`com.nexguard.agent` LaunchAgent on a Mac, the `nexguard-agent`
systemd user service on Linux, the *NexGuard agent* sign-in task on Windows), with no
blocking. It reports as often as the dashboard asks (every 5 minutes by default), through
the relay. It sends CPU, memory and GPU use, battery, free and total storage, system
version, model and computer name. It doesn't read files, see the screen or record
keystrokes.

## Health

The device page's **Health** card shows CPU, memory and GPU use right now, and charts of
CPU, memory, GPU, battery and storage over 6 hours to 30 days. History is kept for
`DEVICE_METRICS_DAYS` (30) and deleted with the device.

| Reading | Mac | Linux | Windows |
|---|---|---|---|
| CPU | `top` (one-second sample) | `/proc/stat` (one-second sample) | `Win32_Processor` load |
| Memory used | active + wired + compressed (as Activity Monitor) | `MemTotal - MemAvailable` | total - free physical |
| GPU | `ioreg` device utilization | AMD `gpu_busy_percent` or `nvidia-smi` | 3D engine counters (English Windows only) |

A reading the system can't give is left out and shows as "Not reported". It isn't
recorded as zero.

Installed without an administrator, the agent runs from:

| System | Runs from | Files |
|---|---|---|
| Windows | Task Scheduler: *NexGuard agent* (at sign-in, no window) | `%LOCALAPPDATA%\NexGuard` |
| Mac | LaunchAgent `com.nexguard.agent` | `~/Library/Application Support/NexGuard`, `~/Library/LaunchAgents/com.nexguard.agent.plist` |
| Linux | systemd user service `nexguard-agent`, or cron if there's no systemd | `~/.local/share/nexguard` |

Its relay login and signing key are in `config.json`, readable only by that user. A failed
check-in shows in `nexguard status` and the log. (Computers set up with the older
check-in script, *NexGuard device check-in*, stopped reporting when computers moved to the
relay: paste the line again and the agent replaces it.)

## The agent

The one-line command installs the NexGuard agent (`device-agent/`) when the dashboard has
builds of it (`device-agent/build.sh` writes them to `device-agent/dist/`, or set
`DEVICE_AGENT_DIR`). Where Go is installed, the dashboard rebuilds them by itself when
`device-agent/` changes (`DEVICE_AGENT_AUTOBUILD`). It downloads the one for that computer, checks it against
`SHA256SUMS`, and asks for the computer's **administrator password** (sudo on a Mac or
Linux, a permission prompt on Windows). It then runs as a protected system service: it
reports health, screen time and websites, blocks websites when they're turned on (below),
and only an administrator can stop or remove it.

Without the administrator password, the command installs the same agent **running as the
person**: a login item on a Mac, a systemd user service on Linux, a scheduled task at
sign-in on Windows. It sees only their own screen and browsers, **can't block websites**,
and the person can remove it. To add blocking later, use **Pair again** and paste the line
again with the password. Without agent builds on the dashboard, the command stops and says
so: the agent is the only computer client.

Computers never talk to the dashboard: the agent pairs and checks in through the same
Redis relay servers use (see [architecture.md](architecture.md#the-relay-protocol)), so
the dashboard only has to be reachable for the link page and the downloads. The relay must
be reachable from the computer at `REDIS_URL`'s host (or `RELAY_PUBLIC_HOST`), and any
Redis 6+ works; phones additionally need Upstash.

## Screen time, websites and blocking (agent only)

On the device page, **Controls → Change** turns each of these on or off. Only for a
computer assigned to a child (family account) or a staff member (company account), or
your own computer in a solo account, and running the agent (blocking only when an
administrator installed it): on another adult's computer, this is how monitoring gets abused, and
the scripts run as the user, who could simply delete them. Pairing tells the person what
can be turned on. Changes reach the computer at its next check-in.

| | What it records | How |
|---|---|---|
| **Screen time** | Minutes in use per day, by app in front | Every minute, while someone is signed in, the screen is unlocked and the keyboard or mouse was used in the last 2 minutes. Mac: `ioreg` idle time and `lsappinfo`. Windows: a hidden helper in the person's session (`nexguard sample`). Linux: `loginctl` sessions; no per-app split (shows as "Desktop"). |
| **Websites visited** | Site (e.g. `youtube.com`), visits and time per day | Every 3 minutes, from Chrome, Edge, Brave, Arc, Vivaldi, Opera, Chromium, Firefox and Safari history, for every user on the computer. The address is cut down to the site **on the computer**: pages and searches are never sent. Only visits after it was switched on. |
| **Block adult websites** | — | About 77,000 sites (`ADULT_BLOCKLIST_URL`) written into the system hosts file. |
| **SafeSearch** | — | Google, Bing, DuckDuckGo and YouTube pointed at their SafeSearch-only addresses. |
| **Also block / Always allow** | — | Your own lists; a site covers its `www.` address too. |

Activity is kept `DEVICE_ACTIVITY_DAYS` (30) days and deleted with the device.

**Location** (Controls → Location): the agent sends a position every few minutes.
- **Windows:** from the Windows location service (Wi-Fi, tens of metres) when *Location
  services* and *Let desktop apps access your location* are on (Settings → Privacy &
  security → Location). No prompt.
- **Linux:** from GeoClue; the administrator install allows itself in GeoClue's
  configuration.
- **Mac, or when the above is off:** no position. A Mac doesn't share one with background
  apps, and since computers check in through the relay, the dashboard never sees their
  internet address to place them by town.

Limits worth knowing:
- Private and incognito windows aren't in the browser's history, so they don't show.
- On a Mac, websites need **Full Disk Access** for `/Library/Application Support/NexGuard/nexguard`
  (System Settings → Privacy & Security → Full Disk Access). macOS protects Safari's
  history, and on recent versions Chrome's and other browsers' too. The connect command
  asks for it on a Mac: it opens that settings page and a Finder window with the agent,
  and waits until it's switched on (Control C skips). Until it's given, the device page
  names the browsers it can't read and shows the steps.
- Blocking is a hosts file: a VPN, a proxy or a browser's own secure DNS can get around
  it, and someone with administrator rights can undo it (the agent puts it back at the
  next check-in). Firefox's automatic DNS-over-HTTPS is switched off through its
  `use-application-dns.net` signal.
- Windows Defender may flag hosts entries that redirect Google or Bing (SafeSearch) as a
  "hosts file hijack" and remove them. If SafeSearch doesn't hold on a Windows computer,
  that's why; adult-site blocking isn't affected.

## Removing a device

- **On the dashboard:** delete the device. Its relay login is deleted, so its next check-in
  is refused; the agent forgets the pairing and removes its blocking.
- **On the computer:** paste one line (also on the link page under *How do I remove it?*):
  `irm https://monitor.example.com/d/uninstall.ps1 | iex` on Windows,
  `curl -fsSL https://monitor.example.com/d/uninstall.sh | sh` on a Mac or Linux.
  The dashboard keeps listing the computer (as offline) until someone deletes it there.

## When something goes wrong

| What they see | Why, and the fix |
|---|---|
| *That code isn't valid* | Each code works once, and making a new one cancels the old. Make a new code on the pair page and send the new link. |
| *This code is for a Mac* (or another system) | The device was added as a different system. Add it again with the right one. |
| *Can't reach …* | The computer can't open the dashboard address (for the download) or the relay. Set `DASHBOARD_URL` to an address it can reach (not `127.0.0.1` unless it's the same machine), and make sure the relay's port is open from there. |
| *The dashboard didn't answer through the relay* | The relay process (`manage.py relay`) isn't running. The code still works: start it and try again. |
| *Too many wrong codes* | 20 wrong codes in 10 minutes, across all devices. Wait, then try again. |
| *Paired with an older NexGuard* | It was paired before computers moved to the relay. Make a new code on the pair page and paste the line again. |
| Paired, but stays offline | Run `nexguard status`: it shows the last check-in and its error. On Linux without systemd or cron, nothing can keep the agent running. |

**See what the agent is doing on the computer itself.** `nexguard status` shows whether it's
paired, its last check-in and where its log is (`logs/nexguard.log` in the agent's folder,
one JSON line per event).

## How it works (and why it's safe)

- The link page and the two scripts need no sign-in and **never look the code up**: they
  only check it's 8 characters from the code alphabet, so they can't be used to test
  codes. The code is only checked by the relay process when the agent sends it, and wrong
  codes are limited there (20 per 10 minutes across all devices).
- The scripts come from this dashboard with its address, the code and the relay's
  **pairing login** (`sm-pc`) filled in. That login can only send pairing requests and read
  answers under a random name the agent picks: it can't read other requests, list keys or
  touch any device's data. The same login goes in every script, so anyone who downloads
  one learns nothing more than that. The addresses must be plain URLs; anything that could
  break out of the quotes is refused. Set `DASHBOARD_URL` so the address is the one
  devices should use.
- The agent asks with a preview first, which shows the account name and what it will see
  **without using the code**. It pairs only after a yes, so saying no changes nothing.
- Pairing gives the computer its own relay login (`sm-d-<id>`), allowed only to push to its
  inbox and read its settings and blocklist. The agent makes a signing key and sends it
  while pairing; check-ins are signed with it, and the agent ignores settings and
  blocklists that aren't, so whoever holds the relay's admin password can't steer it.
- Links and scripts are sent with `Cache-Control: no-store`, `Referrer-Policy:
  no-referrer` and `X-Robots-Tag: noindex`, since the code is in the URL.
- The agent refuses a plain `redis://` relay except on this machine or the local network
  (`NEXGUARD_ALLOW_PLAIN=1` lifts that): the login would cross the internet unencrypted.
- `NEXGUARD_YES=1` answers the question for unattended installs; `NEXGUARD_NO_SCHEDULE=1`
  pairs and checks in once without scheduling anything.
- `agent/nexguard_device.py` is a health-only client in Python (standard library), for
  scripting: `pair --relay 'rediss://sm-pc:…' --code K7PQ-3MXA`, with the relay address
  from the line the link page shows.
