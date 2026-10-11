# The agent and `smctl`

Every monitored server runs the **agent**: one Python file plus `psutil`, managed by systemd
as the unprivileged `server-monitor` user. It reports metrics and service state, and runs
only the actions its local allowlist declares. `smctl` is the command you use on the server
to manage it.

- [Installing](#installing)
- [The allowlist: agent.toml](#the-allowlist-agenttoml)
- [smctl reference](#smctl-reference)
- [What the agent reports](#what-the-agent-reports)
- [Cron jobs and backups (Heartbeat monitors)](#cron-jobs-and-backups-heartbeat-monitors)
- [Uptime checks from your servers](#uptime-checks-from-your-servers)
- [Scheduled and rolling jobs](#scheduled-and-rolling-jobs)
- [Following logs live](#following-logs-live)
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
| `smctl run NAME -- CMD ARGS…` | Run a cron job and report start, success or failure (with its exit code) to the Heartbeat monitor called NAME. Exits with the job's own code. |
| `smctl ping NAME [--fail] [--start] [--exit-code N]` | Report a job's heartbeat from your own script. |
| `smctl shell on\|off` | Allow or forbid free-form shell from the dashboard. |
| `smctl docker enable\|disable` | Let the agent see Docker containers. **The docker group is root-equivalent.** |
| `smctl probes on\|off` | Let the dashboard run HTTP/TCP uptime checks from this server (off by default). |
| `smctl connect` | Install new relay credentials after a rotation (pasted at a hidden prompt). |
| `smctl edit` | Edit `agent.toml` in `$EDITOR`, validated before saving. |
| `smctl sync` | Rewrite the sudo rules from the config. |
| `smctl restart` / `smctl logs [-f]` | Restart the agent / read its log. |
| `smctl upgrade --ref REF --sums SUMS` | Verified upgrade; copy the exact command from the dashboard. |
| `smctl remote-upgrade on\|off` | Allow Admins to upgrade this agent from the dashboard (release tags only; off by default). |
| `smctl uninstall [--purge]` | Remove the agent (and with `--purge`, its config and user). |

Every change is applied immediately, and the dashboard shows it on the next heartbeat.

## What the agent reports

Every heartbeat (every 2 minutes by default) carries CPU, memory, disks, load, network,
uptime and the state and version of each declared service. Since 2.2 it also carries, unless
you set `[agent] inventory = false`:

- **Top processes** by CPU and by memory (10 each),
- **Containers** with their CPU and memory, read from cgroup v2 files, so the agent needs no
  Docker access. `sudo smctl docker enable` adds names and stopped containers via
  `docker ps` (the docker group is root-equivalent, so that's your call),
- **Temperatures** from the kernel's sensors and **NVIDIA GPUs** (`nvidia-smi`, if present).

The dashboard shows them on the server page and alerts when a running container stops, or a
sensor reaches its high (warning) or critical mark. A heartbeat stays under 32 KB.

## Cron jobs and backups (Heartbeat monitors)

Create a monitor of type **Heartbeat** on the dashboard, choose the server and a job name,
and say how often it runs. Then wrap the job on that server, for example in
`/etc/cron.d/backup`:

```cron
0 3 * * * root smctl run nightly-backup -- /opt/backup.sh
```

If no success arrives within the period plus its grace time, or the job reports a failure,
the monitor goes down and you're alerted. Pings are signed with the server's key and travel
through the relay like heartbeats, so nothing needs to reach the dashboard, and a server can
only report its own jobs. Needs agent 2.2.0 or later.

## Uptime checks from your servers

An HTTP or TCP application monitor can also be checked **from your servers**: for internal
URLs the dashboard can't reach (`http://10.0.0.5:8080/health`), or to see an app from several
places at once.

1. On each server that should check: `sudo smctl probes on`.
2. In the monitor's **Where from** section, tick those servers. Tick **Only from the servers
   below** for internal URLs, and set **Down when this many locations fail** (for example 2
   of 3, so one server's network blip doesn't page you).

The agent picks up its checks within one heartbeat, runs each at most once per heartbeat
interval (every 2 minutes by default), and sends the results with its next heartbeat: **no
extra Redis commands**. The monitor page lists every location's latest result; uptime and
alerts follow the quorum. Agents never contact cloud metadata or link-local addresses, and
monitors with secret request headers can only be checked from the dashboard.

## Scheduled and rolling jobs

**Rolling jobs.** On **Run on servers** (or the API), tick **Roll out in batches**: servers
are updated a batch at a time, in name order. After each batch the dashboard waits the pause
you set, then for the health-gate monitors you chose to pass a check made *after* the batch,
and only then starts the next one. If a server fails (or a gate isn't up within 10 minutes)
the job **halts** and later batches don't start; on the job page, **Resume** continues
(accepting the failures so far) and **Abort** cancels the remaining batches.

**Schedules** (Operations → Schedules, Admins) run an action on a cron expression in the
dashboard's `TIME_ZONE`: `0 3 * * *` (daily at 03:00), `30 2 * * mon`, `*/15 * * * *`, or
`@hourly`/`@daily`/`@weekly`. Targets can be servers, groups and environments, resolved each
time it runs, so new servers in a group are included automatically. A schedule can roll out
in batches too. Each run is an ordinary job (marked *Scheduled: name*); if the relay process
was down at the time, a missed run happens once when it's back, not once per missed slot.

## Following logs live

On a server's page, pick a log and press **Follow** (optionally with *Only lines containing*):
new lines stream in for up to 10 minutes, or until you press **Stop**. It survives log
rotation. The list also offers the **journals of declared systemd services**
(`journalctl -u … -f`), for which the `server-monitor` user needs to read the journal: on
Debian/Ubuntu it's in `adm` already; elsewhere add it to `systemd-journal`. Following costs
about one relay command every 3 seconds while it runs. Needs agent 2.2 or later.

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

**Upgrade from the dashboard, no SSH** (opt-in, per server):

```bash
sudo smctl remote-upgrade on
```

Then **⋯ → Upgrade agent now** on the server's page, or **Upgrade all outdated** on the System
page, which upgrades in batches and stops at the first failure. Needs `AGENT_REF` pinned to a
release tag. How it stays safe:

1. The dashboard sends a signed `_upgrade` command carrying its own `AGENT_REF` tag and the
   checksums of the agent files it was deployed with.
2. The agent (unprivileged) only writes `/var/lib/server-monitor-agent/upgrade-request.json`,
   and only if `remote_upgrade = true` in its root-owned config.
3. `server-monitor-upgrade.path` starts the root `server-monitor-upgrade.service`, which runs
   `smctl upgrade --from-request`. It treats the file as untrusted (no symlinks, small
   regular file only), checks the config again, accepts only a `vX.Y.Z` tag and 64-hex
   checksums, always uses the repository recorded at install, and runs the normal verified
   upgrade. The result (with the installer's output) comes back as the job's output.

*Threat note:* someone who controls the `server-monitor` user could request any **release
tag** of your recorded repository, and only with files matching the checksums they supply.
That's no more than the trust you already place in your releases; turn it off with
`sudo smctl remote-upgrade off`.

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
| Following a journal shows nothing | The agent user can't read the journal: `sudo usermod -aG systemd-journal server-monitor && sudo smctl restart`. |
| A monitor says "probes are off on this server" | Run `sudo smctl probes on` on that server; it starts checking within one heartbeat. |
| No containers on the server page | cgroup v1 hosts aren't read; containers appear with `sudo smctl docker enable`. |
| Restart button fails | Run `sudo smctl sync` to rewrite the sudo rules, then `sudo smctl doctor`. |
