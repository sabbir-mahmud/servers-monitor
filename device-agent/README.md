# NexGuard device agent

The agent for Windows, Mac and Linux computers. An administrator installs it once. It
runs as a system service (a Windows service, a macOS LaunchDaemon or a systemd unit),
starts with the computer, and reports health to the dashboard: CPU, memory, GPU,
battery, storage, network (Wi-Fi or wired, and the Wi-Fi name where the system allows)
and system version. The person using the computer can't stop or remove it without
administrator rights. It is one static file with no dependencies.

It never talks to the dashboard: it pairs and checks in through the dashboard's Redis
relay (any Redis 6+, the same one server agents use), so the dashboard can stay private.
The relay address (`rediss://sm-pc:…/?prefix=sm`) is in the line the dashboard's link page
shows; that login can only send pairing requests.

```sh
sudo nexguard pair 'rediss://sm-pc:…@relay.example.com:6379/?prefix=sm' K7PQ-3MXA   # asks y/n
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
  Pairing gives this computer its own relay login and a signing key it made, both kept in
  the config (admin only).
- **Check-ins:** a signed `RPUSH` to its inbox, then a `GET` of the settings (interval,
  controls) and, when its version changed, the blocklist the dashboard left. Anything not
  signed with this computer's key is ignored. When the relay can't be reached it backs
  off, up to 30 minutes between tries.
- **Removed on the dashboard:** its relay login is deleted, so the next check-in is
  refused. The agent then forgets the pairing and waits, and an administrator can pair it
  again. A config from before the relay (`server` and `token`) means: pair again.
- **Plain `redis://`:** allowed only for this machine or the local network, unless
  `NEXGUARD_ALLOW_PLAIN=1` is set.

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
(*NexGuard agent*), the same thing the dashboard's one-line command sets up.
It does health, screen time and websites for that person only, and never touches the
hosts file. Serve the builds from the dashboard by running `./build.sh` (into `dist/`);
a dashboard with Go installed rebuilds them by itself when the source is newer.

**Log**: `logs/nexguard.log` next to the config, one JSON object per line (start, stop,
each check-in, settings the dashboard switched on or off, errors), readable without
administrator rights and rotated at 1 MB into `nexguard.log.1`. It never holds the relay
password or signing key,
website names or app names. The service also still logs to the Windows event log, syslog or the journal.

**Develop**
```sh
go test ./...            # unit tests, with a real health reading from this machine
./build.sh               # all six builds + SHA256SUMS into dist/
make device-agent        # vet, test, build (from the repo root)
```
`main/tests/test_device_agent.py` builds the agent and runs it against a Redis-protocol
relay wired to the dashboard's bridge. It uses `NEXGUARD_CONFIG_DIR` and never installs a
service.
