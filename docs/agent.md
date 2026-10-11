# The agent and `smctl`

Every monitored server runs the **agent**: one Python file plus `psutil`, managed by systemd
as the unprivileged `server-monitor` user. It reports metrics and service state, and runs
only the actions its local allowlist declares. `smctl` is the command you use on the server
to manage it.

- [Installing](#installing)
- [The allowlist: agent.toml](#the-allowlist-agenttoml)
- [smctl reference](#smctl-reference)
- [Deploy actions that need root](#deploy-actions-that-need-root)
- [Upgrading and re-keying](#upgrading-and-re-keying)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)

## Installing

Requirements: Linux with systemd, Python 3.11+, `curl` and `sha256sum` (Ubuntu 22.04+,
Debian 12+, RHEL/Rocky/Alma 9+). The installer adds `python3-venv` if it's missing.

1. In the dashboard, **Servers → Add server**.
2. Copy the command it shows and run it on the server. It looks like:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/sabbir-mahmud/servers-monitor/main/agent/install.sh -o sm-install.sh \
     && echo "<sha256>  sm-install.sh" | sha256sum -c --quiet - \
     && sudo SM_REF=main SM_SUMS=agent=<sha256>,smctl=<sha256>,unit=<sha256> bash sm-install.sh
   ```

3. When asked, paste the **connection string** from the same page. It's read at a hidden
   prompt, so it never appears in `ps` or your shell history.

The installer:

1. checks the installer's own checksum (that's the `sha256sum -c` step),
2. downloads the agent, `smctl` and the systemd unit, refusing any file that doesn't match
   the checksums from your dashboard,
3. logs in to the relay before writing any config,
4. detects running services (nginx, Postgres, Redis, gunicorn, your own units…) and log files,
   and adds them as monitor-only,
5. starts the service and sends one real heartbeat to prove it all works.

The dashboard page switches to **Connected** by itself.

**Unattended installs** (config management, cloud-init): write the connection string to a
root-only file and pass `SM_REDIS_FILE=/path/to/file` to the installer.

## The allowlist: agent.toml

`/etc/server-monitor-agent/agent.toml` (mode 640, owned by root) is the security boundary. The
dashboard can only offer, and the agent will only run, what it declares. Edit it with `smctl`
(below) or `sudo smctl edit`; both validate the result with the agent before saving.
[`agent/agent.example.toml`](../agent/agent.example.toml) documents every option.

```toml
[main]
redis_url = "rediss://sm-<id>:<password>@<host>:6379/?key=<signing key>"

[agent]
report_interval = 120          # the dashboard can change this live

[services.nginx]
kind = "systemd"               # systemd | docker | pm2 | process | http
unit = "nginx"
restartable = true             # shows a Restart button (exact sudo rule added)

[services.api-health]
kind = "http"
url = "http://127.0.0.1:8000/health/"   # internal URLs: checked from the server itself

[logs]
nginx_error = "/var/log/nginx/error.log"

[actions.deploy]
description = "Pull and restart"
command = ["/opt/deploy/deploy.sh"]     # argv list, never a shell string
cwd = "/var/www/app"
timeout = 900

[shell]
enabled = false                # free-form shell from the dashboard (Admin role, audited)
```

## smctl reference

All commands except `--help` need `sudo`.

| Command | What it does |
|---|---|
| `smctl status` | Is the agent running? Which relay? How much is allowed? Recent errors. |
| `smctl doctor` | Checks config, service, relay login, a real heartbeat, log permissions, units and sudo rules, and explains any problem it finds. |
| `smctl detect [--apply]` | Lists services and logs found on this host; `--apply` adds the new ones. |
| `smctl list` | What the dashboard can see and run. |
| `smctl service add NAME [--kind K] [--unit U] [--restartable]` | Monitor a service. |
| `smctl service restartable NAME on\|off` | Enable or disable its Restart button. |
| `smctl service rm NAME` | Stop monitoring it. |
| `smctl log add NAME PATH` / `log rm NAME` | Make a log tail-able from the dashboard. |
| `smctl action add NAME [-d TEXT] [-t SECONDS] [--cwd DIR] [--sudo] -- CMD ARGS…` | Add a one-click action. |
| `smctl action test NAME` | Run it here, as the agent user. |
| `smctl action rm NAME` | Remove it. |
| `smctl shell on\|off` | Allow or forbid free-form shell from the dashboard. |
| `smctl docker enable\|disable` | Let the agent see Docker containers. **The docker group is root-equivalent.** |
| `smctl connect` | Install new relay credentials after a rotation (pasted at a hidden prompt). |
| `smctl edit` | Edit `agent.toml` in `$EDITOR`, validated before saving. |
| `smctl sync` | Rewrite the sudo rules from the config. |
| `smctl restart` / `smctl logs [-f]` | Restart the agent / read its log. |
| `smctl upgrade --ref REF --sums SUMS` | Verified upgrade; copy the exact command from the dashboard. |
| `smctl uninstall [--purge]` | Remove the agent (and with `--purge`, its config and user). |

Every change is applied immediately, and the dashboard shows it on the next heartbeat.

## Deploy actions that need root

Actions run as the unprivileged `server-monitor` user. If a script needs root, add it with
`--sudo`:

```bash
sudo smctl action add upgrade --sudo -- /usr/bin/apt-get -y upgrade
```

`smctl` then writes one `NOPASSWD` line to `/etc/sudoers.d/server-monitor` naming that **exact**
command and arguments, validated with `visudo`. Arguments with spaces, quotes or sudoers
special characters (`, : = \ * ?`) are refused: put that logic in a script instead.
Restartable systemd services get the same treatment (`systemctl restart <unit>`), and unit
names are restricted to letters, digits and `_ . : @ -`.

## Upgrading and re-keying

**Upgrade the agent:** on the server's page, open **⋯ → Install / upgrade agent** and run the
verified upgrade command shown there (`sudo smctl upgrade --ref … --sums …`). The config is
kept.

**Rotate credentials** (lost connection string, staff change, or a legacy server enrolled
before command signing): on the server's page, **⋯ → Rotate credentials**, then on the server:

```bash
sudo smctl connect      # paste the new connection string when asked
```

The old Redis password stops working immediately. The new connection string also carries a
new command-signing key.

## Uninstalling

```bash
sudo smctl uninstall            # keeps /etc/server-monitor-agent
sudo smctl uninstall --purge    # removes config and the server-monitor user too
```

Then delete the server in the dashboard, which also deletes its Redis user.

## Troubleshooting

Start with `sudo smctl doctor`. It names the problem and the fix. Common ones:

| Symptom | Cause and fix |
|---|---|
| Dashboard says **Waiting for the agent** | The dashboard's **relay process** isn't running: start it (`make dev`, `docker compose up -d relay`, or `systemctl start server-monitor-relay`). A banner warns you when it's down. |
| `relay refused the credentials` | Credentials were rotated or the server was deleted/disabled. Rotate and `sudo smctl connect`. |
| `cannot reach the relay` | Outbound TCP 6379 (or your provider's port) is blocked, or DNS fails. |
| `doesn't match what the dashboard expects` during install | GitHub has different agent files than your dashboard was deployed with. Redeploy the dashboard from the same commit, or pin `AGENT_REF` to a tag. |
| `Rejected an unsigned or forged message` in the agent log | Something wrote to the relay without the signing key. Commands from your dashboard are always signed; investigate who has your Redis admin credentials. |
| A log can't be tailed | The `server-monitor` user can't read it. Add the user to the file's group (it's already in `adm` on Debian/Ubuntu). |
| Restart button fails | Run `sudo smctl sync` to rewrite the sudo rules, then `sudo smctl doctor`. |
