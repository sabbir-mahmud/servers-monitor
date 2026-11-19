# NexGuard desktop app

The app for Windows, macOS and Linux that **connects a computer to a NexGuard dashboard**
and then shows what the NexGuard agent on it is doing: whether it's connected, when it last
checked in, and its log, live. Accounts pair computers through this app by default
(dashboard: *Account settings → Pairing*).

## Connecting a computer

1. The parent or admin adds the computer on the dashboard and sends the link it shows
   (`https://monitor.example.com/d/K7PQ-3MXA`).
2. On the computer, install this app, open the link and choose **Open in NexGuard**. That's a
   `nexguard://pair?server=…&code=…` link, and it fills in the pair screen. Or open the app
   (File → *Connect this computer…*) and type the dashboard address and code.
3. The app asks the dashboard what it will see (a preview, which doesn't use the code) and
   shows it word for word. Nothing is installed until the person chooses **Allow and
   connect**.
4. It runs the agent that ships inside the app (`Resources/agent/nexguard`):
   - **Protected install** (default): `nexguard pair --yes --via app <dashboard> <code>`,
     then `nexguard install`, as an administrator. macOS asks with its password dialog,
     Windows with UAC, Linux with `pkexec`. The agent runs as a system service that only an
     administrator can stop, and can block websites.
   - **Protected install off**: `nexguard --user --dir <folder> pair …`, then
     `… install`, with no administrator. The agent runs as that person while they're signed
     in (LaunchAgent, systemd user service or sign-in task), with no website blocking.
5. The window switches to the agent's status and log.

A pairing link only fills in the form. The person always sees the disclosure and has to
choose Allow. Addresses must be `https://`, or `http://` on this machine or the local
network, as the agent requires.

## Status and log

The app reads two files the agent keeps readable for everyone. It needs no administrator
rights and no network for this:

| | Protected install | Installed for one person (`--user`) |
|---|---|---|
| Windows | `%ProgramData%\NexGuard\` | `%LOCALAPPDATA%\NexGuard\` |
| macOS | `/Library/Application Support/NexGuard/` | `~/Library/Application Support/NexGuard/` |
| Linux | `/etc/nexguard/` | `$XDG_DATA_HOME/nexguard/` (`~/.local/share/nexguard/`) |

- `status.json`: the last check-in (time, OK or the error, interval)
- `logs/nexguard.log`: one JSON object per line, rotated at 1 MB into `nexguard.log.1`.
  The agent never logs its pairing token, website names or app names.

The window shows:
- A connection pill: Connected, Last check-in failed (with the error), or Quiet when
  check-ins stopped.
- New lines live, within a second. Scrolling up pauses following.
- Level filters (Info, Warnings, Errors) and search (Ctrl/Cmd+F).
- **Save** and **Show file**.

`NEXGUARD_CONFIG_DIR` points the app at another agent folder (and pairs into it), the same as
for the agent.

## Install

Download from the [releases](https://github.com/sabbir-mahmud/servers-monitor/releases)
tagged `desktop-v…`. The dashboard links there too (`DESKTOP_APP_URL`).

| System | File |
|---|---|
| Windows 10/11 | `NexGuard-<v>-win-x64.exe` (or `-arm64`) |
| macOS 12+ | `NexGuard-<v>-mac-arm64.dmg` (Apple silicon) or `-x64.dmg` (Intel) |
| Linux | `.AppImage` (`chmod +x`, run) or `.deb` (`sudo apt install ./NexGuard-….deb`) |

The installers register `nexguard://` links. With the AppImage, open it once so the
desktop learns about it. Unsigned builds: on macOS, right-click the app → Open the first
time. On Windows, choose *More info → Run anyway*. Add the signing secrets listed in
`.github/workflows/desktop-app.yml` to sign and notarise.

## Develop

```sh
make device-agent                 # builds device-agent/dist, which the app uses in development
cd desktop-app && npm install
npm start                         # the agent from ../device-agent/dist
NEXGUARD_CONFIG_DIR=/tmp/ng npm start          # another agent folder; pairing goes there too
NEXGUARD_AGENT_BIN=/path/to/nexguard npm start # another agent program
npm test                          # node:test, no Electron needed
npm run dist:mac                  # or dist:win / dist:linux: copies the agent builds into
                                  # agent-bin/ (checked against SHA256SUMS), then packages
```

| | |
|---|---|
| `src/main.js` | Window, menu, `nexguard://` links, IPC. Sandboxed renderer, no navigation, no new windows. |
| `src/agent.js` | Preview against the dashboard; runs the bundled agent to pair and install (elevated or `--user`). |
| `src/pairing.js` | Code and address checks, link parsing, the elevated command per system (quoting tested). |
| `src/paths.js` | Where the agent lives on each system (mirrors `device-agent/internal/config`). |
| `src/tail.js`, `src/parse.js` | Reading and following the log, through rotation and half-written lines. |
| `src/preload.js` | The small API the page gets through `contextBridge`. |
| `src/renderer/` | The page: plain HTML/CSS/JS with a strict CSP. Text from the log or the dashboard is only set with `textContent`. |
| `scripts/agent-bin.js` | Puts each system's agent build where electron-builder bundles it. |

Releasing: bump `version` in `package.json` and push a tag `desktop-v<version>`. The
*Desktop app* workflow builds the agent, then the installers on macOS, Windows and Linux, and
attaches them with `SHA256SUMS` to a GitHub Release.
