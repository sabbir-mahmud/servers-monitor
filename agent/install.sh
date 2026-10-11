#!/usr/bin/env bash
# Install or upgrade the Server Monitor agent.
#
#   curl -fsSL https://monitor.example.com/agent/files/install.sh \
#     | sudo SM_URL=https://monitor.example.com SM_TOKEN=smk_xxx bash
#
# Re-running upgrades the agent and keeps the existing agent.toml.
set -euo pipefail

SM_URL="${SM_URL:-}"
SM_TOKEN="${SM_TOKEN:-}"
PREFIX=/opt/server-monitor-agent
CONF_DIR=/etc/server-monitor-agent
CONF="$CONF_DIR/agent.toml"
AGENT_USER=server-monitor

die() { echo "error: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ -n "$SM_URL" ] || die "SM_URL is required"
command -v systemctl >/dev/null || die "systemd is required"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python >= 3.11 required (tomllib)"
python3 -c 'import venv' 2>/dev/null || die "python3-venv is required (apt install python3-venv)"

echo "==> user"
if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$AGENT_USER"
fi
# Read /var/log/* (Debian/Ubuntu); harmless where the group doesn't exist.
getent group adm >/dev/null && usermod -aG adm "$AGENT_USER" || true

echo "==> files"
install -d -m 755 "$PREFIX"
curl -fsSL "$SM_URL/agent/files/server_monitor_agent.py" -o "$PREFIX/server_monitor_agent.py.new"
python3 -m py_compile "$PREFIX/server_monitor_agent.py.new"
mv "$PREFIX/server_monitor_agent.py.new" "$PREFIX/server_monitor_agent.py"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade "psutil>=6,<8"

echo "==> config"
install -d -m 750 -o root -g "$AGENT_USER" "$CONF_DIR"
if [ ! -f "$CONF" ]; then
  [ -n "$SM_TOKEN" ] || die "SM_TOKEN is required for a first install"
  curl -fsSL "$SM_URL/agent/files/agent.example.toml" -o "$CONF"
  # Fill in url + token; leave the example services/logs/actions for the operator to edit.
  python3 - "$CONF" "$SM_URL" "$SM_TOKEN" <<'PY'
import re, sys
path, url, token = sys.argv[1:]
text = open(path).read()
text = re.sub(r'^url = ".*"$', f'url = "{url}"', text, count=1, flags=re.M)
text = re.sub(r'^token = ".*"$', f'token = "{token}"', text, count=1, flags=re.M)
if url.startswith("http://"):
    text = text.replace('# allow_insecure_http = true', 'allow_insecure_http = true', 1)
open(path, "w").write(text)
PY
  # The example declares services/actions this host may not have; start minimal and safe.
  python3 - "$CONF" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
head, sep, _ = text.partition("# ---------------------------------------------------------------- services")
minimal = head + sep + """
# Declare services, logs and actions here — see the annotated example:
#   SM_URL/agent/files/agent.example.toml

[logs]

[shell]
enabled = false
"""
open(path, "w").write(minimal)
PY
  chown root:"$AGENT_USER" "$CONF"
  chmod 640 "$CONF"
else
  echo "    keeping existing $CONF"
fi

echo "==> service"
curl -fsSL "$SM_URL/agent/files/server-monitor-agent.service" -o /etc/systemd/system/server-monitor-agent.service
systemctl daemon-reload
systemctl enable --now server-monitor-agent
systemctl restart server-monitor-agent
sleep 2
systemctl --no-pager --lines=5 status server-monitor-agent || true

cat <<MSG

Installed. Next:
  1. Edit $CONF to declare services, logs and deploy actions.
  2. For restart buttons, allow the agent user to restart only those units, e.g.
       echo '$AGENT_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx' \\
         > /etc/sudoers.d/server-monitor && chmod 440 /etc/sudoers.d/server-monitor
  3. sudo systemctl restart server-monitor-agent
MSG
