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

Phones use the NexGuard app instead: the pair page shows a QR code to scan.

## What gets installed

A check-in that runs **as the signed-in user** every 5 minutes and reports as often as the
dashboard asks. It sends CPU, memory and GPU use, battery, free and total storage, system
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

| System | Runs from | Files |
|---|---|---|
| Windows | Task Scheduler: *NexGuard device check-in* (at sign-in, then every 5 min, no window) | `%LOCALAPPDATA%\NexGuard` |
| Mac | LaunchAgent `com.nexguard.device` | `~/Library/Application Support/NexGuard`, `~/Library/LaunchAgents/com.nexguard.device.plist` |
| Linux | systemd user timer `nexguard-device.timer`, or cron if there's no systemd | `~/.local/share/nexguard` |

The device token is stored in a `config` file readable only by that user. A failed
check-in is noted in an `error` file next to it.

## The agent

The one-line command installs the NexGuard agent (`device-agent/`) when the dashboard has
builds of it (`device-agent/build.sh` writes them to `device-agent/dist/`, or set
`DEVICE_AGENT_DIR`). It downloads the one for that computer, checks it against
`SHA256SUMS`, and asks for the computer's **administrator password** (sudo on a Mac or
Linux, a permission prompt on Windows). It then runs as a protected system service: it
reports health, screen time and websites, blocks websites when they're turned on (below),
and only an administrator can stop or remove it.

Without the administrator password, the command installs the same agent **running as the
person**: a login item on a Mac, a systemd user service on Linux, a scheduled task at
sign-in on Windows. It sees only their own screen and browsers, **can't block websites**,
and the person can remove it. To add blocking later, use **Pair again** and paste the line
again with the password. Without agent builds on the dashboard, the command installs the
health-only check-in script instead.

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
- **Mac, or when the above is off:** the town its internet connection is in, from the free
  [DB-IP](https://db-ip.com) city database the dashboard downloads monthly
  (`DEVICE_GEOIP_URL`). Looked up on the dashboard; no address is sent anywhere. A VPN
  shows the VPN's town, and a dashboard reached over a home network or `localhost` sees
  no public address, so nothing is shown. Behind a reverse proxy, set
  `TRUST_PROXY_HEADERS`.

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

- **On the dashboard:** delete the device. Its next check-in gets "not paired" and it
  uninstalls itself.
- **On the computer:** paste one line (also on the link page under *How do I remove it?*):
  `irm https://monitor.example.com/d/uninstall.ps1 | iex` on Windows,
  `curl -fsSL https://monitor.example.com/d/uninstall.sh | sh` on a Mac or Linux.
  The dashboard keeps listing the computer (as offline) until someone deletes it there.

## When something goes wrong

| What they see | Why, and the fix |
|---|---|
| *That code isn't valid* | Each code works once, and making a new one cancels the old. Make a new code on the pair page and send the new link. |
| *This code is for a Mac* (or another system) | The device was added as a different system. Add it again with the right one. |
| *Can't reach …* | The computer can't open the dashboard address. Set `DASHBOARD_URL` to an address it can reach (not `127.0.0.1` unless it's the same machine). |
| *Too many tries* | 10 attempts per 10 minutes per network. Wait, then try again. |
| Paired, but stays offline | Look at the `error` file in the folder above. On Linux without systemd or cron, nothing can schedule the check-in. |

## How it works (and why it's safe)

- The link page and the two scripts need no sign-in and **never look the code up**: they
  only check it's 8 characters from the code alphabet, so they can't be used to test
  codes. The code is only checked by `POST /device/v1/pair`, which is rate-limited.
- The scripts come from this dashboard with its address and the code filled in. The
  address must be a plain `https://host[:port]/path`; anything that could break out of the
  quotes is refused. Set `DASHBOARD_URL` so the address is the one devices should use.
- The installer asks with `"preview": true` first, which shows the account name and what
  it will see **without using the code**. It pairs only after a yes, so saying no changes
  nothing.
- Links and scripts are sent with `Cache-Control: no-store`, `Referrer-Policy:
  no-referrer` and `X-Robots-Tag: noindex`, since the code is in the URL.
- Over plain `http://` (a dashboard on your home network) the page warns to do this only
  on a trusted network: the token would cross it unencrypted.
- The shell installer reads the dashboard's answers as `key=value` lines
  (`Accept: text/plain`), so it needs nothing beyond `sh`, `curl`, `sed` and `awk`.
  `NEXGUARD_YES=1` answers the question for unattended installs; `NEXGUARD_NO_SCHEDULE=1`
  pairs and checks in once without scheduling anything.
- `agent/nexguard_device.py` is the same client in Python, for scripting and as a
  reference for the phone apps.
