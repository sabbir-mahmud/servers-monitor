# NexGuard device agent

The agent for Windows, Mac and Linux computers. An administrator installs it once. It
runs as a system service (a Windows service, a macOS LaunchDaemon or a systemd unit),
starts with the computer, and reports health to the dashboard: CPU, memory, GPU,
battery, storage, network (Wi-Fi or wired, and the Wi-Fi name where the system allows)
and system version. The person using the computer can't stop or remove it without
administrator rights. It is one static file with no dependencies. The [NexGuard desktop app](../desktop-app/README.md)
carries it and runs these commands for the person, so they never need a terminal.

```sh
sudo nexguard pair https://monitor.example.com K7PQ-3MXA   # shows who will see it, asks y/n
sudo nexguard install      # copies itself into place, starts the service
nexguard status            # paired? service running? last check-in?
sudo nexguard checkin      # check in once now
sudo nexguard uninstall    # stop and remove everything
```

On Windows, run the same commands from an administrator PowerShell (`nexguard.exe`).

| | Windows | macOS | Linux |
|---|---|---|---|
| Service | `nexguard` (LocalSystem, automatic, restarts on failure) | `/Library/LaunchDaemons/nexguard.plist` (KeepAlive) | `nexguard.service` (Restart=always) |
| Binary | `%ProgramFiles%\NexGuard\nexguard.exe` | `/Library/Application Support/NexGuard/nexguard` | `/usr/local/bin/nexguard` |
| Config (admin only) | `%ProgramData%\NexGuard\config.json` | `/Library/Application Support/NexGuard/config.json` | `/etc/nexguard/config.json` |

**Behaviour**
- **Pairing:** `pair` asks the dashboard first with a preview, which doesn't use the code.
  It pairs only after a yes. `--yes` skips the question when the person was told another way.
- **Check-ins:** at the interval the dashboard asks for. When the dashboard can't be
  reached it backs off, up to 30 minutes between tries.
- **Removed on the dashboard:** the next check-in gets "not paired". The agent then forgets
  the pairing and waits, and an administrator can pair it again.
- **Plain `http://`:** allowed only for this machine or the local network, unless
  `NEXGUARD_ALLOW_HTTP=1` is set.

**Screen time, websites and blocking** (a child's or staff computer, switched on in the
dashboard; see [docs/devices.md](../docs/devices.md)):
- Screen time: one look a minute (Windows: a hidden `nexguard sample` helper in the
  signed-in person's session, because the service can't see their screen).
- Websites: browser history read every 3 minutes from a copy of each history database
  (pure-Go SQLite), reduced to the site on this computer. Cursors in `activity.json` mean
  each visit counts once; nothing from before it was switched on is sent.
- Blocking: a managed section in the hosts file between `# >>> NexGuard` markers. Lines
  outside it are never touched. It's re-checked at every check-in and removed when blocking
  is switched off, the device is removed on the dashboard, or on `uninstall`.
- Unsent activity waits in `activity.json` (admin only) and goes as a numbered batch, so a
  lost answer doesn't count anything twice.

**Without administrator rights** (`--user`): `nexguard --user --dir <folder> install`
copies this program into that folder and runs it as the person with
`nexguard --user --dir <folder> run`: a LaunchAgent (`com.nexguard.agent`), a systemd user
service (`nexguard-agent`, or an autostart entry without systemd) or a sign-in task
(*NexGuard agent*). The desktop app and the dashboard's one-line command set up the same
thing, so either can replace the other.

**Pairing through the app**: `pair --via app` tells the dashboard the desktop app showed the
disclosure. Accounts set to *app only* (the default) refuse computers that pair without it.
It does health, screen time and websites for that person only, and never touches the
hosts file. Serve the builds from the dashboard by running `./build.sh` (into `dist/`).

**Log**: `logs/nexguard.log` next to the config, one JSON object per line (start, stop,
each check-in, settings the dashboard switched on or off, errors), readable without
administrator rights and rotated at 1 MB into `nexguard.log.1`. It never holds the token,
website names or app names. The [NexGuard desktop app](../desktop-app/README.md) shows it live. The service also still logs to the Windows event log, syslog or the journal.

**Develop**
```sh
go test ./...            # unit tests, with a real health reading from this machine
./build.sh               # all six builds + SHA256SUMS into dist/
make device-agent        # vet, test, build (from the repo root)
```
`main/tests/test_device_agent.py` builds the agent and runs it against a live dashboard.
It uses `NEXGUARD_CONFIG_DIR` and never installs a service.
