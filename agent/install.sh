#!/usr/bin/env bash
# Install or upgrade the Server Monitor agent: one command, fully connected.
#
#   curl -fsSL https://raw.githubusercontent.com/sabbir-mahmud/servers-monitor/main/agent/install.sh \
#     | sudo SM_REDIS='rediss://sm-<id>:<password>@<host>:6379' bash
#
# The dashboard's "Add server" page prints this command with the server's own credentials.
# Nothing connects in: the agent dials out to the Redis relay over TLS, and the dashboard
# does the same from wherever it runs (even a laptop with no public address).
#
#   1. installs python3 + venv if missing (apt, dnf or yum)
#   2. downloads the agent and `smctl` from GitHub (SM_REPO, SM_REF)
#   3. logs in to the relay with SM_REDIS before touching the config
#   4. detects services and logs on this host and adds them (monitor-only)
#   5. starts the systemd service and confirms a real heartbeat
#
# Re-running upgrades the agent and smctl and keeps the existing config.
# Env: SM_REDIS (first install), SM_REF (branch/tag, default main), SM_REPO, SM_DETECT=0.
set -euo pipefail

SM_REDIS="${SM_REDIS:-}"
SM_REPO="${SM_REPO:-sabbir-mahmud/servers-monitor}"
SM_REF="${SM_REF:-main}"
SM_DETECT="${SM_DETECT:-1}"
SRC="https://raw.githubusercontent.com/$SM_REPO/$SM_REF/agent"
PREFIX=/opt/server-monitor-agent
CONF_DIR=/etc/server-monitor-agent
CONF="$CONF_DIR/agent.toml"
AGENT_USER=server-monitor

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'; else B= G= R= Y= D= N=; fi
step() { printf '\n%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$R" "$*" "$N" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: pipe into 'sudo ... bash'"
command -v systemctl >/dev/null || die "systemd is required"
command -v curl >/dev/null || die "curl is required"
FIRST_INSTALL=0; [ -f "$CONF" ] || FIRST_INSTALL=1
if [ "$FIRST_INSTALL" = 1 ] && [ -z "$SM_REDIS" ]; then
  die "SM_REDIS is required: copy the full command from the dashboard's Add server page"
fi

# ------------------------------------------------------------------ 1. prerequisites
step "Prerequisites"
pkg_install() {
  if command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
  elif command -v dnf >/dev/null; then dnf install -y -q "$@" >/dev/null
  elif command -v yum >/dev/null; then yum install -y -q "$@" >/dev/null
  else die "please install: $*"; fi
}
if ! command -v python3 >/dev/null; then
  command -v apt-get >/dev/null && apt-get update -qq >/dev/null
  pkg_install python3
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "Python 3.11+ is required (found $(python3 -V 2>&1)). Ubuntu 24.04 / Debian 12 / RHEL 9 ship it."
if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get update -qq >/dev/null
    PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    pkg_install "python${PYV}-venv" || pkg_install python3-venv
  else
    pkg_install python3-pip
  fi
fi
ok "$(python3 -V) with venv"

# ------------------------------------------------------------------ 2. download
step "Downloading the agent from github.com/$SM_REPO ($SM_REF)"
if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$AGENT_USER"
fi
# Read /var/log/* on Debian/Ubuntu; harmless where the group doesn't exist.
getent group adm >/dev/null && usermod -aG adm "$AGENT_USER" || true
install -d -m 755 "$PREFIX"
fetch() {  # fetch NAME DEST: download, syntax-check Python, then swap in atomically
  curl -fsSL --retry 3 "$SRC/$1" -o "$2.new" || die "could not download $1 from $SRC (is the repo public and the ref right?)"
  case "$1" in *.py) python3 -m py_compile "$2.new" || die "$1 failed to compile on $(python3 -V 2>&1)" ;; esac
  mv "$2.new" "$2"
}
fetch server_monitor_agent.py "$PREFIX/server_monitor_agent.py"
fetch smctl.py "$PREFIX/smctl.py"
rm -rf "$PREFIX/__pycache__"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "psutil>=6,<8"
install -m 755 "$PREFIX/smctl.py" /usr/local/bin/smctl
printf 'SM_REPO=%s\nSM_REF=%s\n' "$SM_REPO" "$SM_REF" > "$PREFIX/source"  # for `smctl upgrade`
ok "agent $(grep -m1 '^VERSION' "$PREFIX/server_monitor_agent.py" | cut -d'"' -f2) in $PREFIX"
ok "smctl installed: run 'sudo smctl' to manage this server"

# ------------------------------------------------------------------ 3. relay + config
write_config() {  # write_config PATH REDIS_URL
  local insecure=false; case "$2" in redis://*) insecure=true ;; esac
  (umask 027; cat > "$1" <<TOML
[main]
redis_url = "$2"
verify_tls = true
allow_insecure = $insecure

[agent]
report_interval = 120
max_concurrent_commands = 2
disk_mounts = []
use_sudo_for_restart = true

[logs]

[shell]
enabled = false
TOML
)
}
step "Connecting to the relay"
if [ "$FIRST_INSTALL" = 1 ]; then
  case "$SM_REDIS" in redis://*|rediss://*) ;; *) die "SM_REDIS must start with rediss://" ;; esac
  TMP_CONF=$(mktemp)
  trap 'rm -f "$TMP_CONF"' EXIT
  write_config "$TMP_CONF" "$SM_REDIS"
  "$PREFIX/venv/bin/python" "$PREFIX/server_monitor_agent.py" -c "$TMP_CONF" --ping >/dev/null 2>"$TMP_CONF.err" \
    || die "$(cat "$TMP_CONF.err"). Rotate the credentials on the server's page and use the new command."
  rm -f "$TMP_CONF.err"
  ok "credentials accepted by the relay"
  install -d -m 750 -o root -g "$AGENT_USER" "$CONF_DIR"
  install -m 640 -o root -g "$AGENT_USER" "$TMP_CONF" "$CONF"
  ok "wrote $CONF"
  if [ "$SM_DETECT" = 1 ]; then
    smctl detect --apply --no-restart || printf '  %s!%s detection failed; add services later with smctl\n' "$Y" "$N"
  fi
else
  ok "keeping existing $CONF"
fi

# ------------------------------------------------------------------ 4. start + verify
step "Starting"
fetch server-monitor-agent.service /etc/systemd/system/server-monitor-agent.service
systemctl daemon-reload
systemctl enable server-monitor-agent >/dev/null 2>&1
systemctl restart server-monitor-agent
sleep 2
systemctl is-active --quiet server-monitor-agent || die "the agent failed to start: journalctl -u server-monitor-agent -n 30"
# One real heartbeat, as the agent user: proves config, permissions and the relay login.
if runuser -u "$AGENT_USER" -- "$PREFIX/venv/bin/python" "$PREFIX/server_monitor_agent.py" -c "$CONF" --once >/dev/null 2>&1; then
  ok "agent running and reporting"
else
  printf '  %s!%s agent running, but a test heartbeat failed. Diagnose with: sudo smctl doctor\n' "$Y" "$N"
fi

cat <<MSG

${G}${B}Done.${N} This server now shows up in the dashboard and reports every 2 minutes.
No SSH, no open ports: jobs you queue reach it through the relay within a second.

${B}Manage it with smctl:${N}
  sudo smctl list                                    ${D}what the dashboard can see and run${N}
  sudo smctl action add deploy -- /opt/deploy.sh     ${D}add a one-click deploy${N}
  sudo smctl service restartable nginx on            ${D}enable the Restart button${N}
  sudo smctl log add app /var/www/app/logs/app.log   ${D}make a log tail-able${N}
  sudo smctl doctor                                  ${D}diagnose problems${N}
  smctl --help                                       ${D}everything else${N}
MSG
