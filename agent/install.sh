#!/usr/bin/env bash
# Install or upgrade the Server Monitor agent: one command, fully connected.
#
#   curl -fsSL https://monitor.example.com/agent/files/install.sh \
#     | sudo SM_URL=https://monitor.example.com SM_TOKEN=smk_xxx bash
#
# What it does:
#   1. installs python3/venv if missing (apt, dnf or yum)
#   2. checks the dashboard is reachable and the token is accepted, before changing anything
#   3. installs the agent (own system user + venv) and the `smctl` management command
#   4. detects services and logs on this host and adds them (monitor-only)
#   5. starts the systemd service and confirms the first heartbeat arrived
#
# Re-running upgrades the agent and smctl and keeps the existing config.
# Options (env): SM_DETECT=0 skips auto-detection.
set -euo pipefail

SM_URL="${SM_URL:-}"
SM_URL="${SM_URL%/}"
SM_TOKEN="${SM_TOKEN:-}"
SM_DETECT="${SM_DETECT:-1}"
PREFIX=/opt/server-monitor-agent
CONF_DIR=/etc/server-monitor-agent
CONF="$CONF_DIR/agent.toml"
AGENT_USER=server-monitor

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'; else B= G= R= Y= D= N=; fi
step() { printf '\n%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$R" "$*" "$N" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: pipe into 'sudo ... bash'"
[ -n "$SM_URL" ] || die "SM_URL is required (copy the command from the dashboard's Add server page)"
command -v systemctl >/dev/null || die "systemd is required"
command -v curl >/dev/null || die "curl is required"
FIRST_INSTALL=0; [ -f "$CONF" ] || FIRST_INSTALL=1

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

# ------------------------------------------------------------------ 2. preflight
step "Connecting to $SM_URL"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$SM_URL/agent/files/install.sh" || true)
[ "$code" = "200" ] || die "cannot reach $SM_URL (HTTP ${code:-none}). Check DNS, outbound firewall and TLS."
ok "dashboard reachable"
if [ "$FIRST_INSTALL" = 1 ]; then
  [ -n "$SM_TOKEN" ] || die "SM_TOKEN is required for a first install"
  # An empty heartbeat is rejected as 400 when the token is good and 401 when it isn't; it
  # changes nothing on the dashboard either way.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST \
    -H "Authorization: Bearer $SM_TOKEN" -H 'Content-Type: application/json' -d '{}' \
    "$SM_URL/api/agent/v1/heartbeat/" || true)
  case "$code" in
    400|200) ok "token accepted" ;;
    401|403) die "the dashboard rejected this token. It is shown once: rotate it on the server's page and use the new one." ;;
    *) die "unexpected answer from the dashboard (HTTP $code)" ;;
  esac
fi

# ------------------------------------------------------------------ 3. install
step "Installing the agent"
if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$AGENT_USER"
fi
# Read /var/log/* on Debian/Ubuntu; harmless where the group doesn't exist.
getent group adm >/dev/null && usermod -aG adm "$AGENT_USER" || true
install -d -m 755 "$PREFIX"
fetch() {  # fetch NAME DEST: download, syntax-check Python, then swap in atomically
  curl -fsSL "$SM_URL/agent/files/$1" -o "$2.new"
  case "$1" in *.py) python3 -m py_compile "$2.new" ;; esac
  mv "$2.new" "$2"
}
fetch server_monitor_agent.py "$PREFIX/server_monitor_agent.py"
fetch smctl.py "$PREFIX/smctl.py"
rm -rf "$PREFIX/__pycache__"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "psutil>=6,<8"
install -m 755 "$PREFIX/smctl.py" /usr/local/bin/smctl
ok "agent $(grep -m1 '^VERSION' "$PREFIX/server_monitor_agent.py" | cut -d'"' -f2) in $PREFIX"
ok "smctl installed: run 'sudo smctl' to manage this server"

# ------------------------------------------------------------------ 4. config
step "Configuring"
install -d -m 750 -o root -g "$AGENT_USER" "$CONF_DIR"
if [ "$FIRST_INSTALL" = 1 ]; then
  INSECURE=false; case "$SM_URL" in http://*) INSECURE=true ;; esac
  umask 027
  cat > "$CONF" <<TOML
[main]
url = "$SM_URL"
token = "$SM_TOKEN"
verify_tls = true
allow_insecure_http = $INSECURE

[agent]
report_interval = 30
command_poll_interval = 5
max_concurrent_commands = 2
disk_mounts = []
use_sudo_for_restart = true

[logs]

[shell]
enabled = false
TOML
  chown root:"$AGENT_USER" "$CONF"; chmod 640 "$CONF"
  ok "wrote $CONF"
  if [ "$SM_DETECT" = 1 ]; then
    smctl detect --apply --no-restart || printf '  %s!%s detection failed; add services later with smctl\n' "$Y" "$N"
  fi
else
  ok "keeping existing $CONF"
fi

# ------------------------------------------------------------------ 5. start + verify
step "Starting"
fetch server-monitor-agent.service /etc/systemd/system/server-monitor-agent.service
systemctl daemon-reload
systemctl enable server-monitor-agent >/dev/null 2>&1
systemctl restart server-monitor-agent
systemctl is-active --quiet server-monitor-agent || die "the agent failed to start: journalctl -u server-monitor-agent -n 30"
# One real heartbeat, as the agent user: proves config, permissions, network and token.
if runuser -u "$AGENT_USER" -- "$PREFIX/venv/bin/python" "$PREFIX/server_monitor_agent.py" -c "$CONF" --once >/dev/null 2>&1; then
  ok "agent running and reporting"
else
  printf '  %s!%s agent running, but a test heartbeat failed. Diagnose with: sudo smctl doctor\n' "$Y" "$N"
fi

cat <<MSG

${G}${B}Done.${N} This server now appears in the dashboard and updates every 30 seconds.
No SSH or inbound ports needed: the agent dials out and picks up jobs you queue.

${B}Manage it with smctl:${N}
  sudo smctl list                                    ${D}what the dashboard can see and run${N}
  sudo smctl action add deploy -- /opt/deploy.sh     ${D}add a one-click deploy${N}
  sudo smctl service restartable nginx on            ${D}enable the Restart button${N}
  sudo smctl log add app /var/www/app/logs/app.log   ${D}make a log tail-able${N}
  sudo smctl doctor                                  ${D}diagnose problems${N}
  smctl --help                                       ${D}everything else${N}
MSG
